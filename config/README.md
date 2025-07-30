# Configuration Files

## PPC Campaigns Configuration

The `ppc_campaigns.json` file contains the configuration for PPC (Pay-Per-Click) campaign reporting.

### IMPORTANT: Exact Campaign Name Matching

**The PPC reporting system uses EXACT campaign name matching. Campaign names must match exactly what appears in Google Analytics 4.**

### Setting Up Campaign Names

1. **Replace placeholder names**: The file contains placeholder campaign names like `REPLACE_WITH_EXACT_CAMPAIGN_NAME_1`
2. **Get exact names from GA4**: Copy the exact campaign names from your Google Analytics 4 reports
3. **Case-sensitive matching**: Campaign names are case-sensitive - "Brand Campaign" is different from "brand campaign"
4. **No wildcards or patterns**: Only exact matches are tracked

### Example Configuration

```json
{
  "campaign_name": "TryBooking UK - Brand - Search",
  "source": "google",
  "medium": "cpc",
  "platform": "google_ads",
  "active": true
}
```

In this example, only conversions where `firstUserCampaignName` in GA4 exactly equals "TryBooking UK - Brand - Search" will be tracked.

### Structure

- **campaigns**: Array of campaign definitions with the following fields:
  - `campaign_name`: EXACT campaign name as it appears in GA4
  - `source`: Traffic source (google or bing only)
  - `medium`: Traffic medium (typically cpc or ppc)
  - `platform`: Advertising platform (google_ads or bing_ads)
  - `active`: Boolean flag to enable/disable campaign in reports

- **utm_parameters**: Valid UTM parameter values:
  - `utm_source`: Limited to google and bing
  - `utm_medium`: Limited to cpc and ppc

- **reporting_settings**: Default settings for report generation:
  - `default_date_range`: Default time period for reports
  - `timezone`: Timezone for date calculations (Europe/London)
  - `currency`: Currency for monetary values (GBP)
  - `exclude_internal_traffic`: Filter out internal traffic
  - `exact_match_only`: Enforces exact campaign name matching (set to true)

### Updating Campaigns

To add or update campaigns:
1. Get the EXACT campaign name from Google Analytics 4 or your ad platform
2. Replace placeholder names in the campaigns array
3. Ensure source is either "google" or "bing"
4. Set `active: true` to include in reports
5. Commit and push changes