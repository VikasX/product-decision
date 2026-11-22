"""One-shot LangChain adapter; no agent, retries, repair calls or model fallbacks."""

import httpx
from langchain_core.exceptions import ModelError
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from langsmith import tracing_context
from openai import APIError, APIStatusError

from app.core.config import Settings
from app.integrations.llm import AttemptCharge, prepare_request, validate_evidence
from app.schemas.analysis import Judgment, Product, Review
from app.services.usage import MODEL_PRICES, CostProtectionError


def prepare_langchain_request(config: Settings, product: Product, reviews: list[Review]):
    # Reuse the same investigation instructions, evidence preparation and schema.
    payload, amount = prepare_request(config, product, reviews)
    # Reserve additional framing for LangChain/OpenAI's native schema serialization.
    # The wire-shape bound is checked against captured mock transport requests in tests.
    input_price, output_price = MODEL_PRICES[config.openai_model]
    input_bound = (amount - config.openai_max_output_tokens * output_price) // input_price + 4096
    if input_bound > config.openai_max_input_tokens:
        raise CostProtectionError("Review evidence exceeds the configured AI input size limit.")
    return payload, amount + 4096 * input_price


async def analyze_reviews_langchain(
    config: Settings, product: Product, reviews: list[Review], charge: AttemptCharge, store
) -> Judgment:
    if not config.openai_api_key:
        raise CostProtectionError("Configure the OpenAI API key before running AI analysis.")
    payload, amount = prepare_langchain_request(config, product, reviews)
    prompt = ChatPromptTemplate.from_messages(
        [("system", "{instructions}"), ("human", "{evidence}")]
    )
    async with httpx.AsyncClient(timeout=45) as async_client:
        with httpx.Client(timeout=45) as sync_client:
            model = ChatOpenAI(
                model=config.openai_model,
                api_key=config.openai_api_key,
                base_url="https://api.openai.com/v1",
                reasoning_effort="minimal",
                max_completion_tokens=config.openai_max_output_tokens,
                temperature=None,
                max_retries=0,
                timeout=45,
                n=1,
                streaming=False,
                stream_usage=False,
                cache=False,
                use_responses_api=False,
                http_client=sync_client,
                http_async_client=async_client,
            )
            chain = prompt | model.with_structured_output(
                Judgment, method="json_schema", strict=True
            )
            store.reject_bad_credential()
            store.reserve(amount)
            charge.reserved = amount
            try:
                # Do not inherit LangSmith tracing settings or transmit reviews to a tracer.
                with tracing_context(enabled=False):
                    judgment = await chain.ainvoke(
                        {
                            "instructions": payload["messages"][0]["content"],
                            "evidence": payload["messages"][1]["content"],
                        },
                        config={"callbacks": []},
                    )
            except APIStatusError as exc:
                if exc.status_code == 401:
                    store.record_auth_failure()
                # Reuse the service's sanitized HTTP status handling, never SDK error bodies.
                raise httpx.HTTPStatusError(
                    "LangChain model request failed",
                    request=exc.response.request,
                    response=exc.response,
                ) from None
            except (APIError, ModelError):
                raise ValueError("LangChain model response was unavailable or refused") from None
    judgment = Judgment.model_validate(judgment)
    validate_evidence(judgment, reviews)
    return judgment
