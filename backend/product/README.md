# Saved product catalog

Demo Mode reads only datasets explicitly listed in `catalog.json`. A folder of
screenshots alone does not enable a category or product. LED Lights contains Gesto
(`prd1`); Headphones contains boAt Rockerz 411 (`prd2`, saved in `headphone/prd1`).
Product IDs are unique across categories, even when their folder names match.
Mobiles contains Samsung Galaxy A56 5G (`prd3`, saved in `mobile/prd1`).

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

## Samsung Galaxy A56 evidence verification

- Details: `193935` capture; Awesome Violet Glow, 8 GB RAM, 256 GB storage, ₹45,999.
- Histogram: 1,473 ratings, average 4.4; 5→1 star percentages 65/22/4/2/7.
- Topic counts: Quality 240+/37−, Value 80+/25−, Camera 53+/20−,
  Performance 38+/14−, Display 33+/6−, Appearance 35+/0−,
  Battery 38+/31−, Heat 8+/18−. Battery and Heat retain the visible mixed labels.
- The dataset retains 32 partial topic excerpts, not identifiable full reviews.
  Review claims about OS versions/charging are reported text, not verified specifications.
- `url.txt` supplies the canonical product identity. Original captures are archived
  outside Git at `D:/code/amazon/source-evidence/mobile/prd1`.
- The local product photo comes from Samsung India's Awesome Violet Glow gallery:
  https://images.samsung.com/is/image/samsung/assets/in/Combo_720x480_M_Awesome-Light-Violet.jpg
  Other product facts and ratings come only from the user's screenshots, not Samsung's live ratings.

## Additional saved products
Each record below is transcribed from the user-provided screenshots. Images are standalone assets; external pages were used only to locate matching photos, not to replace screenshot prices or ratings.
- prd7: One94Store Smart RGB Strip; 1,554 ratings, 3.9/5; histogram 5→1: 56/16/9/4/15; 8 topics and 16 selected partial excerpts.
- prd8: Noise Master Buds; 1,738 ratings, 3.7/5; histogram 5→1: 53/13/7/4/23; 8 topics and 16 selected partial excerpts.
- prd4: Dell 15 Core 3 Laptop; 427 ratings, 4.1/5; histogram 5→1: 62/17/8/1/12; 5 topics and 10 selected partial excerpts.
- prd5: Primebook 2 Neo; 599 ratings, 4.2/5; histogram 5→1: 67/15/5/3/10; 8 topics and 16 selected partial excerpts.
- prd6: LG 65-inch NU87 4K TV; 5,585 ratings, 3.9/5; histogram 5→1: 53/20/8/3/16; 8 topics and 16 selected partial excerpts.

Original screenshot filenames remain in each JSON dataset; originals are archived at D:/code/amazon/source-evidence with the same category/product folder structure. No full review identities or hardcoded AI verdicts were added.

Product photo sources:
- mobile/prd2: https://static.enkelhq.com/2b74095a3740c3c6/ablj4abeljn9ww9tcfjzwvipjssjdalusguqvw8s.jpg (located through PriceDecode; Burgundy Red matches the supplied ASIN).
- laptop/prd1: https://m.media-amazon.com/images/I/41HGF8UTv2L._SL500_.jpg
- laptop/prd2: https://m.media-amazon.com/images/I/41231ixLNCL.jpg
- tv/prd1: https://m.media-amazon.com/images/I/51q8e8JjPCL.jpg
- led/prd2: https://m.media-amazon.com/images/I/518WYOX-3yL.jpg
- headphone/prd2: https://m.media-amazon.com/images/I/41Q+mHveFyL.jpg

## OnePlus N6x evidence verification

- prd9, mobile/prd2: 531 ratings, 3.6/5; histogram 5→1: 48/17/7/5/23; ₹22,999; 4 GB RAM and 128 GB storage.
- Eight topic counts (positive/negative): Appearance 5/1, Quality 59/52, Display 3/4, Value 13/37, Camera 2/36, Reliability 4/23, Performance 6/19, Battery 5/16.
- Sixteen readable partial excerpts retain their source screenshot names. They are not identifiable full reviews and do not supply review stars or dates.
- The details screenshot inconsistently shows Ice Blue in the title/photo and Burgundy Red in its colour label. The supplied ASIN also identifies Burgundy Red. The dataset records this limitation and omits a verified colour specification.
- Original captures are preserved outside Git in D:/code/amazon/source-evidence/mobile/prd2. No paid analysis was used to prepare this dataset.
