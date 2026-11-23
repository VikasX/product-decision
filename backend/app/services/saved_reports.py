"""Permanent, source-validated reports for the public catalog; no network or database calls."""

import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field

from app.integrations.llm import validate_evidence
from app.schemas.analysis import AnalysisResponse, Judgment
from app.schemas.demo import DemoDataset
from app.services.demo import DemoCatalog, Workflow
from app.services.usage import PROMPT_VERSION


class SavedReport(BaseModel):
    # Bind a genuine model result to the exact evidence that produced it.
    model_config = ConfigDict(extra="forbid")
    product_id: str
    dataset_version: int = Field(ge=1)
    dataset_sha256: str
    model: str = Field(pattern=r"^gpt-5-nano(?:-2025-08-07)?$")
    workflow: Workflow
    prompt_version: str
    report: AnalysisResponse


def dataset_digest(dataset: DemoDataset) -> str:
    # Include every saved field, so an unversioned edit cannot silently reuse an old report.
    return hashlib.sha256(dataset.model_dump_json().encode()).hexdigest()


def validate_report(saved: SavedReport, dataset: DemoDataset) -> AnalysisResponse:
    """Return a report only when identity, evidence and all model citations still validate."""
    report = saved.report
    if (saved.product_id != dataset.id or saved.dataset_version != dataset.version
            or saved.dataset_sha256 != dataset_digest(dataset)
            or saved.prompt_version != PROMPT_VERSION):
        raise ValueError("Saved analysis does not match the current dataset")
    if report.product != dataset.product or report.reviews != dataset.reviews:
        raise ValueError("Saved analysis source data does not match")
    # A genuine uncertain model verdict is still a valid analysis. Keep it distinct
    # from transport/budget failures and never rewrite it into a positive verdict.
    expected_status = "insufficient_evidence" if report.verdict == "insufficient_evidence" else "completed"
    if report.status != expected_status:
        raise ValueError("Analysis status and verdict disagree")
    if any(line.startswith("AI analysis failed") or line == "No new AI request was made."
           for line in report.coverage.limitations):
        raise ValueError("Cannot publish a failed analysis")
    # Rebuild the original strict LLM output instead of trusting the looser API envelope alone.
    fields = {name: getattr(report, name) for name in Judgment.model_fields}
    fields["recurring_issues"] = [issue.model_dump(exclude={"mention_count"}) for issue in report.recurring_issues]
    judgment = Judgment.model_validate(fields)
    validate_evidence(judgment, dataset.reviews, dataset.product)
    # Public readers need the report, not the private account's budget totals.
    return report.model_copy(deep=True, update={"cached": True, "usage": None})


class SavedReportCatalog(DemoCatalog):
    """Read valid analysis.json files; this report index does not control catalog membership."""

    def report_path(self, product_id: str):
        manifest = json.loads(self.safe_path("catalog.json").read_text(encoding="utf-8"))
        for category in manifest["categories"]:
            for relative in category["datasets"]:
                path = self.safe_path(relative)
                if DemoDataset.model_validate_json(path.read_text(encoding="utf-8")).id == product_id:
                    return path.with_name("analysis.json")
        raise KeyError(product_id)

    def report_for(self, dataset: DemoDataset) -> AnalysisResponse:
        saved = SavedReport.model_validate_json(self.report_path(dataset.id).read_text(encoding="utf-8"))
        return validate_report(saved, dataset)

    def datasets(self):
        ready = []
        for name, dataset in super().datasets():
            try:
                self.report_for(dataset)
            except (OSError, ValueError):
                # A missing, stale or invalid report is an unpublished product, never an AI cache miss.
                continue
            ready.append((name, dataset))
        return ready

    def publish(self, dataset: DemoDataset, report: AnalysisResponse, *, model: str, workflow: Workflow):
        # Private tooling calls this only after obtaining an actual validated model result.
        saved = SavedReport(product_id=dataset.id, dataset_version=dataset.version,
                            dataset_sha256=dataset_digest(dataset), model=model,
                            workflow=workflow, prompt_version=PROMPT_VERSION, report=report)
        saved.report = validate_report(saved, dataset)
        destination = self.report_path(dataset.id)
        # Replace atomically so a reader cannot observe half-written JSON.
        temporary = destination.with_suffix(".json.tmp")
        temporary.write_text(saved.model_dump_json(indent=2) + "\n", encoding="utf-8")
        temporary.replace(destination)
