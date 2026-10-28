# octopia-sync

Updates the **content** of existing Cdiscount listings (title, description, rich description, attributes, pictures) through the Octopia API. Channable only sends content the first time it sees an EAN, so this tool covers later updates. Channable's Cdiscount V2 channel still handles price, stock and shipping.

## Setup

```bash
py -m pip install -r requirements.txt
copy .env.example .env   # then fill in the values
```

- **Octopia credential:** create a separate API credential for this tool in Octopia. Don't reuse the one Channable uses.
- **Channable export:** a custom export in the same project as the Cdiscount channel, as CSV (`,` or `;`) or JSON. The columns are listed below.

| Column | Octopia field | Notes |
|---|---|---|
| `ean` | `gtin` | 8–13 digits, required |
| `sku` | `sellerProductReference` | |
| `category_code` | `categoryCode` | optional; if empty, the product's current Octopia category is used |
| `brand` | `brand` | required when the category has `isBrandMandatory` |
| `title_fr` | `title` | max 132 characters |
| `description_fr` | `description` | HTML is fine: the script converts it to plain text and cuts it to 2000 characters at a sentence end. If empty, `rich_description_fr` is used instead |
| `rich_description_fr` | `richMarketingDescription` | max 5000 characters, limited HTML |
| `variant_group` | `variantGroupReference` | required in `isVariant` categories |
| `image` | `sellerPictureUrls` | main picture (index 1), HTTPS |
| `additional_image_link` | `sellerPictureUrls` | the other pictures; a list is fine (JSON array, or separated by `,`, `\|` or spaces). Duplicates are removed and the total is capped at 6 |
| `image_1` … `image_6` | `sellerPictureUrls` | alternative to the two columns above; empty columns are skipped |
| `attr_<propertyReference>` | `attributes[]` | put several values in one cell, separated by `\|` |

To use different column names, change the mapping in `COLUMNS` in `source_channable.py`.

## Usage

```bash
py sync.py --check-permissions      # rights, current category and mandatory attributes; sends nothing
py sync.py --list-attributes        # all attributes of Voldt's categories → attributes.csv
py sync.py --dry-run                # validate, then print the payload; sends nothing
py sync.py --ean 8720589000017      # send one product, even if it hasn't changed
py sync.py                          # send only products that changed since the last run
py sync.py --all                    # resend everything
py sync.py --no-wait                # submit without waiting for the reports
py sync.py --status                 # fetch results for products still open from earlier runs; sends nothing
```

`--source <url|file>` overrides `CHANNABLE_EXPORT_URL`. `--dry-run` also works without Octopia credentials, but then the category checks are skipped.

`--list-attributes` needs only the Octopia credentials, not the export. It looks up every category Voldt has products in and writes all their attributes to `attributes.csv` (`;`-separated, so it opens directly in Excel). The file has one row per attribute, with:
- the column name to use in Channable (`attr_<ref>`)
- the label in FR and EN
- Mandatory/Recommended/Optional
- whether multiple values are allowed
- the unit
- the allowed French values (`choices_fr`)

`--check-permissions` lists each EAN with its edit/enrich rights and current category. For each category it then shows the French and English names and the mandatory attributes (`attr_<ref>`, with FR/EN label), plus how many products in the export are still missing each one.

## How it works

1. Reads the export and maps each row to an Octopia product. Fetches the current products from `GET /products`. EANs that are not on Octopia are skipped, because v1 never creates products. An empty `category_code` is filled in with the product's current category. If the export's code differs from the current one, a warning is shown.
2. Checks attribute values against the category's properties from `GET /categories/{ref}/properties`. Three kinds of value are left out of the payload, while the rest of the product is still sent:
   - a property the category doesn't have (the export shares one set of `attr_` columns across all categories)
   - a value that isn't in the French `choices`
   - a second value for a property that allows only one

   A value that differs from a choice only in case or spacing is rewritten to the official spelling. Every run prints a summary per attribute of what was left out, and the JSONL log records it per EAN (`AttributesDropped`): that is the list to fix in Channable.
3. Validates locally:
   - field lengths
   - no HTML in `description`
   - 1–6 HTTPS pictures
   - category rules from `GET /categories/{ref}` and `/properties`: mandatory attributes, brand, variant group
4. Diff: hashes each payload and compares it with `state.json`. Only new or changed EANs are sent.
5. A product without `permissions.edit` is skipped and counts as an error.
6. `POST /products-integration` in batches of `BATCH_SIZE` (default 50), with 30 s between batches.
7. Polls `GET /products-integration-reports?packageId=…` at 5, 15, 30 and 60 s, then every 60 s, for at most 10 minutes. It stops once every product has the status `Integrated` or `Refused`. The report pages with `pageIndex`/`pageSize`, not with a cursor. Products still open after 10 minutes stay `Pending`; `--status` or the next run picks them up.
8. Writes the result to `state.json` and to `logs/run-*.jsonl`. Each log line holds the timestamp, EAN, status, operationType, packageId, errors and warnings.

A product refused for its content keeps its hash, so it is only resent once its row in the export changes. A product refused with `QuotaExceeded` is different: Octopia's submission quota is undocumented, and in practice about 50 products went through before it started refusing. Such a product gets the status `QuotaExceeded` and is resent unchanged by the next run, so for a large catalogue you simply run `py sync.py` again later. Use `--ean` to force a resend. `Integrated` means Octopia accepted the content. Cdiscount moderation still follows, and the Seller Zone can lag by hours up to a day.

Exit codes: `0` OK (products refused with `QuotaExceeded` count as OK, because the next run retries them), `1` refused for content or locally invalid products, `2` fatal error such as missing configuration or an API error.

## Scheduled run (GitHub Actions)

`.github/workflows/sync.yml` runs `python sync.py` every hour at :23 UTC. It commits the updated `state.json` back to `main`, and the run log is kept as an artifact for 30 days.

- **Secrets** (Settings → Secrets and variables → Actions): `OCTOPIA_CLIENT_ID`, `OCTOPIA_CLIENT_SECRET`, `OCTOPIA_SELLER_ID` and `CHANNABLE_EXPORT_URL`. Until they are set, every run skips the sync with a warning.
- **Manual run:** Actions → *Octopia content sync* → *Run workflow*. In the `args` field you can pass, for example, `--dry-run`, `--status`, `--all` or `--ean 8721082977739`.
- **Failures:** a run fails (and GitHub e-mails you) when products are refused for their content or are invalid locally. `QuotaExceeded` does not fail a run, because the next hourly run retries those products.
- **State:** `state.json` lives in the repo. Run the sync from GitHub only; if you do run it locally, `git pull` first and push `state.json` afterwards. Otherwise the two copies drift apart and products get sent twice.

## Tests

```bash
py -m unittest discover -s tests -v
```

## Open points (from the handover)

- Is there a sandbox? If not, test with a single EAN first (`--ean`) and check the result in the Seller Zone.
- Find out the propertyReferences of Voldt's categories and line the export columns up with them.
- The submission quota (`QuotaExceeded`) isn't documented: find out from Octopia how many products per hour or per day are allowed.
- Shopify CDN URLs that change trigger a resend and a new round of moderation.
