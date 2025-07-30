# Configuration Files

## PPC Campaigns Configuration

The `ppc_campaigns.json` file contains the configuration for PPC (Pay-Per-Click) campaign reporting.

### Structure

- **campaigns**: Array of campaign definitions with the following fields:
  - `campaign_name`: Display name of the campaign
  - `source`: Traffic source (e.g., google, facebook, bing)
  - `medium`: Traffic medium (e.g., cpc, display, social)
  - `category`: Campaign category (e.g., brand, generic, competitor)
  - `platform`: Advertising platform (e.g., search, display, social)
  - `region`: Target region (e.g., uk)
  - `active`: Boolean flag to enable/disable campaign in reports

- **utm_parameters**: Valid UTM parameter values for campaign tracking:
  - `utm_source`: List of valid traffic sources
  - `utm_medium`: List of valid traffic mediums
  - `utm_campaign_patterns`: Common patterns found in campaign names

- **reporting_settings**: Default settings for report generation:
  - `default_date_range`: Default time period for reports
  - `timezone`: Timezone for date calculations (Europe/London)
  - `currency`: Currency for monetary values (GBP)
  - `exclude_internal_traffic`: Filter out internal traffic
  - `minimum_sessions_threshold`: Minimum sessions to include in report

### Usage

This configuration is used by the PPC reporting workflow to:
1. Identify and categorise PPC traffic from GA4
2. Generate performance reports by campaign type
3. Calculate ROI and conversion metrics
4. Compare performance across different platforms and categories

### Updating Campaigns

To add a new campaign:
1. Add a new object to the `campaigns` array
2. Ensure all required fields are populated
3. Set `active: true` to include in reports
4. Update the workflow if new sources/mediums are added