# Daily Panchangam WhatsApp Automation

Sends today's Panchangam (Tithi, Nakshatram, Rahu Kalam, Yamagandam, Gulika
Kalam, Amrit Kalam / Amruta Ghadiyalu, etc.) for Tirupati, Andhra Pradesh as
three WhatsApp images (English, Telugu, Tamil) every day at 5:30 AM IST —
running entirely on GitHub's free servers, no laptop required.

## One-time setup (~10 minutes)

1. **Create a free GitHub account** at github.com, if you don't have one.
2. **Create a new repository**: click the "+" in the top right → "New
   repository" → name it e.g. `panchangam-automation` → make it **Private**
   (recommended, since it will hold your API key) → Create repository.
3. **Upload these files**, keeping the folder structure exactly as-is:
   - `panchangam_daily.py`
   - `requirements.txt`
   - `fonts/` (all 4 font files inside)
   - `.github/workflows/panchangam.yml`

   Easiest way: on the new repo's page, click "Add file" → "Upload files",
   then drag the whole extracted folder's contents in. GitHub preserves
   subfolders when you drag a folder in via the browser.
4. **Add your secrets**: go to the repo's **Settings** tab → **Secrets and
   variables** → **Actions** → **New repository secret**. Add two secrets:
   - `TEXTMEBOT_APIKEY` → your TextMeBot API key
   - `RECIPIENT_NUMBERS` → the WhatsApp number(s) to send to, with country
     code, comma-separated if more than one (e.g. `+919246998931`)
5. **Enable Actions**: go to the **Actions** tab of the repo. If prompted,
   click "I understand my workflows, go ahead and enable them".
6. **Test it manually**: still on the Actions tab, click "Daily Panchangam
   WhatsApp Send" in the left sidebar → "Run workflow" → "Run workflow"
   button. Wait ~30-60 seconds, then refresh — click into the run to see
   the log (it prints exactly what it parsed from Drik Panchang, which is
   useful for troubleshooting). Check WhatsApp to confirm all 3 images
   arrived.

That's it — from then on it runs automatically every day at 5:30 AM IST,
whether or not your laptop is on.

## Notes

- **Location**: currently set to Tirupati, Andhra Pradesh (geoname-id
  1254360 on drikpanchang.com). To change city, add a `GEONAME_ID` secret
  with the right geoname-id (look it up by finding your city's Drik
  Panchang page and copying the `geoname-id` from its URL).
- **WhatsApp groups**: TextMeBot's API does not support sending to WhatsApp
  groups by default — you need to email them (contact info on
  textmebot.com) to get group-sending enabled for your account. Once
  enabled, group IDs can be used in `RECIPIENT_NUMBERS` the same way as
  phone numbers.
- **Costs**: this is 100% free — GitHub Actions gives free minutes on
  private repos (2,000/month), and this job takes under a minute a day.
- **If a field ever shows as "-"**: Drik Panchang occasionally changes
  their page layout. Check the Actions run log for a `WARNING: could not
  find fields` line — it tells you exactly which field to look at. Message
  me and I can help patch the parser.
- **Editing the design**: `panchangam_daily.py` has a `render_card()`
  function with all the colors/layout — easy to tweak the look and resend.
