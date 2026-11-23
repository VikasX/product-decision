# Saved product catalog

Demo Mode reads only datasets explicitly listed in `catalog.json`. A folder of
screenshots alone does not enable a category or product. LED Lights contains Gesto
(`prd1`); Headphones contains boAt Rockerz 411 (`prd2`, saved in `headphone/prd1`).
Product IDs are unique across categories, even when their folder names match.

## Add a product

1. Create `category-id/prd2/` (then `prd3`, etc.; product IDs must be globally unique).
2. Prepare a `dataset.json`, following `led/prd1/dataset.json`. Keep original screenshots
   outside the repository; their filenames remain in the JSON as source provenance.
3. Set `id`, `category_id`, `version`, product metadata, source filenames and limitations.
   Use a confirmed canonical Amazon URL/ASIN. Unknown optional values stay null.
4. Include full written reviews only when text, source identity and metadata can be
   honestly attributed. Topic-panel snippets go in `topic_excerpts`, not `reviews`.
5. Add the dataset's relative path to its category in `catalog.json`. Create a new
   category entry with an ID/name only when it has a verified dataset.
6. Increment `version` whenever evidence changes. Cache keys also include a content
   hash, workflow, model and prompt version, so edits cannot reuse an older judgment.

Pydantic validates datasets when read. Invalid identities, duplicate star buckets,
contradictory topic counts and invalid screenshot references are rejected. Catalog
paths must stay inside this folder. Browsing and loading evidence make no AI calls.
Pressing Analyze sends the compact summary/topics/reviews through the existing
guarded nano adapter. Full reports cache for 24 hours; failures cache for 60 seconds.
Vercel always uses the existing shared MongoDB store; local auto mode retains SQLite.

## Gesto evidence verification

- Details: `205149` screenshot; observed price ₹399, Bohemian, 5 metres, 5V USB.
- Histogram: 374 ratings, average 3.4, 5→1 star percentages 44/12/12/6/26.
- Topic counts: Appearance 9+/3−, Quality 38+/41−, Value 24+/13−,
  Reliability 8+/17−, Brightness 10+/10−, Color 8+/7−,
  Bluetooth 6+/18−, Adhesive 4+/16−. All are explicitly visible, not estimates.
- Amazon labels Appearance positive, Bluetooth/Adhesive negative, the other topics mixed.
- The eight topic descriptions and excerpts retain their screenshot filename.
  Excerpts have no identifiable review IDs/dates/stars and are not treated as full reviews.
- Gesto ASIN/URL comes from the user's earlier supplied link, not visible screenshot text.
- Raw screenshots are not bundled. Each enabled dataset points to a small local
  `product.jpg` for its card. Source filenames identify original captures kept outside this repo.

The existing `product` folder was moved inside `backend` so Vercel can bundle it.
No live scraping, automatic paid verification, hardcoded verdict or alternate model
is used by Demo Mode. The local LangChain option keeps the existing Lab access policy.
