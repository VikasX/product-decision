"""One-shot LangChain adapter; no agent, retries, repair calls or model fallbacks."""

import httpx
from langchain_core.exceptions import ModelError
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from langsmith import tracing_context
from openai import APIError, APIStatusError
from pydantic import ValidationError

from app.core.config import Settings
from app.integrations.llm import (
    AttemptCharge,
    log_schema_failure,
    prepare_request,
    validate_judgment_evidence,
)
from app.schemas.analysis import Judgment, Product, Review
from app.services.usage import MODEL_PRICES, CostProtectionError


# Build the shared prompt without sending it; include a conservative SDK framing allowance.
def prepare_langchain_request(config: Settings, product: Product, reviews: list[Review]):
    # Reuse the same investigation instructions, evidence preparation and schema.
    # Reuse the HTTP instructions and compact evidence so comparisons concern adapters rather than different inputs.
    payload, amount = prepare_request(config, product, reviews)
    # Reserve additional framing for LangChain/OpenAI's native schema serialization.
    # The wire-shape bound is checked against captured mock transport requests in tests.
    # Read the same model pricing used by the HTTP implementation.
    input_price, output_price = MODEL_PRICES[config.openai_model]
    # Recover the input allowance and add room for the SDK structured-output wrapper.
    input_bound = (amount - config.openai_max_output_tokens * output_price) // input_price + 4096
    if input_bound > config.openai_max_input_tokens:
        raise CostProtectionError("Review evidence exceeds the configured AI input size limit.")
    # Return the shared content plus its larger conservative SDK cost reservation.
    return payload, amount + 4096 * input_price


# LangChain path: ChatPromptTemplate -> ChatOpenAI -> structured Judgment, with one invocation.
async def analyze_reviews_langchain(
    config: Settings, product: Product, reviews: list[Review], charge: AttemptCharge, store
) -> Judgment:
    if not config.openai_api_key:
        raise CostProtectionError("Configure the OpenAI API key before running AI analysis.")
    # Reuse the HTTP instructions and compact evidence so comparisons concern adapters rather than different inputs.
    payload, amount = prepare_langchain_request(config, product, reviews)
    # Variables prevent braces in review text from being interpreted as prompt template syntax.
    prompt = ChatPromptTemplate.from_messages(
        [("system", "{instructions}"), ("human", "{evidence}")]
    )
    # Provide a bounded async transport to ChatOpenAI instead of using an unconfigured default client.
    async with httpx.AsyncClient(timeout=45) as async_client:
        # The SDK also accepts a synchronous client; close it when this adapter finishes.
        with httpx.Client(timeout=45) as sync_client:
            # Match HTTP time/model limits; disable retries, streaming and SDK result caching.
            model = ChatOpenAI(
                model=config.openai_model,
                api_key=config.openai_api_key,
                base_url="https://api.openai.com/v1",
                reasoning_effort="minimal",
                max_completion_tokens=config.openai_max_output_tokens,
                temperature=None,
                # Disable SDK retries so a transient failure cannot silently create another paid attempt.
                max_retries=0,
                timeout=45,
                n=1,
                streaming=False,
                stream_usage=False,
                # Use our explicit MongoDB/SQLite cache, not an additional LangChain model cache.
                cache=False,
                use_responses_api=False,
                http_client=sync_client,
                http_async_client=async_client,
            )
            # Pydantic structured output checks shape; shared source validation still runs afterward.
            chain = prompt | model.with_structured_output(
                payload["response_format"]["json_schema"]["schema"] if not reviews else Judgment,
                method="json_schema",
                strict=True,
            )
            store.reject_bad_credential()
            # Use the same shared app budget guard as HTTP before invoking the chain.
            store.reserve(amount)
            charge.reserved = amount
            try:
                # Do not inherit LangSmith tracing settings or transmit reviews to a tracer.
                # Disable external tracing even if tracing variables exist in the environment.
                with tracing_context(enabled=False):
                    # Await one prompt/model invocation; supplied variables are filled into the template as data.
                    judgment = await chain.ainvoke(
                        {
                            "instructions": payload["messages"][0]["content"],
                            "evidence": payload["messages"][1]["content"],
                        },
                        config={"callbacks": []},
                    )
            # The OpenAI SDK uses its own HTTP exception type; convert it for existing service error handling.
            except APIStatusError as exc:
                if exc.status_code == 401:
                    store.record_auth_failure()
                # Reuse the service's sanitized HTTP status handling, never SDK error bodies.
                raise httpx.HTTPStatusError(
                    "LangChain model request failed",
                    request=exc.response.request,
                    response=exc.response,
                ) from None
            # Convert other SDK/model errors into the service existing invalid-response failure path.
            except ValidationError as exc:
                log_schema_failure(exc)
                raise ValueError("LangChain output did not match the analysis schema") from None
            except (APIError, ModelError):
                raise ValueError("LangChain model response was unavailable or refused") from None
    # Ensure the returned chain value still satisfies the shared output contract.
    judgment = Judgment.model_validate(judgment)
    # Run the same source checks as HTTP; structured output alone does not verify facts.
    validate_judgment_evidence(judgment, reviews, product)
    return judgment
