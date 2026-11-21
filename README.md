# product-decision

A shopper pastes an Amazon India product URL and gets a buy or skip recommendation with the reviews that explain it. When evidence is insufficient, the app says what is missing.

## First milestone

The first URL-to-analysis flow is implemented and running locally. Track completed
work, validation and remaining gaps in [Project status](STATUS.md). Backend setup,
API details and usage limits are in [the backend guide](backend/README.md).

Use the Gesto LED strip (ASIN `B0D5Q7RWF6`) as the first worked example. A user should be able to submit the link, read a verdict, see how many written reviews were analysed, and inspect the excerpts behind each reason.

1. **Check the review source.** Try retrieving the listing and written reviews for this ASIN. Record the source, variant, fields available, accessible star levels and dates, and whether another page can be fetched. Record access failures too. If access is limited, use the supplied screenshots as a labelled development sample while evaluating sources.
2. **Define the result with that sample.** Store each review with an ID, text, stars, date, variant and source reference where available. Keep the listing's total ratings separate from the number of unique reviews analysed. Each reason in the result carries the IDs of its supporting reviews.
3. **Connect one complete flow.** Add a FastAPI analysis endpoint under `/api/v1` and connect the existing Next.js form. Have the model return a verdict, reasons and evidence IDs. Check that cited IDs exist, and show the original excerpts. Show an explicit state for unavailable or insufficient evidence.
4. **Check the decision by hand.** On the screenshot sample, look for repeated colour failures, mixed experiences with adhesive and the unrelated phone-charging review. The report should exclude the charging claim and avoid treating the one-star share as a defect rate. Repeat with other products before relying on the verdict.

Once the basic flow works, let the model request one additional review batch to resolve a specific uncertainty, such as whether recent reviews of the same variant repeat an issue. Record what the follow-up contributed before expanding the agent.
