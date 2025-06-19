# Churn Risk Model Implementation Guide

## Quick Start

The enhanced churn risk model is already integrated into `zoho_tiers.py`. When the script runs, it automatically:

1. Loads event creation dates from the Accounts report
2. Analyzes booking patterns to classify account types
3. Calculates risk scores using the three-tier system
4. Updates Zoho CRM with scores and flags at-risk accounts

## Configuration

### Environment Variables Required
```bash
# AWS (for S3 access)
AWS_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY

# Zoho CRM
ZOHO_CLIENT_ID
ZOHO_CLIENT_SECRET
ZOHO_REFRESH_TOKEN

# Email (for reports)
MAILGUN_SMTP_LOGIN
MAILGUN_SMTP_PASSWORD
MAILGUN_DOMAIN

# Optional
TEST_MODE=true  # Prevents Zoho updates, sends test emails
```

### Data Dependencies

The model requires three S3 files:
1. `YYYYMM01-BookingDataAll-TBUK.csv` - Historical transactions
2. `YYYYMM-BookingData-TBUK.csv` - Current month transactions
3. `YYYYMM-Accounts-TBUK.csv` - Account metadata with creation dates

## How the Model Works

### 1. Pattern Detection Phase
```python
# For each account, the model:
- Reads FirstEventCreated and LastEventCreated
- Analyzes booking history to find event clusters
- Classifies pattern: Annual, Seasonal, Regular, Occasional, or Struggling
```

### 2. Risk Calculation Phase
```python
# Three-tier assessment:
1. Creation Activity (40 pts) - Days since LastEventCreated
2. Revenue Performance (35 pts) - Decline % and percentile
3. Struggle Indicators (25 pts) - Events not selling, free events
```

### 3. Output Phase
- Updates Zoho CRM with Churn_Risk scores (0-100)
- Generates at-risk accounts report (emailed)
- Saves detailed CSV for analysis

## Understanding Risk Scores

### Score Ranges
- **0-20**: Healthy - No action needed
- **21-40**: Low Risk - Monitor quarterly  
- **41-60**: Moderate Risk - Check-in recommended
- **61-80**: High Risk - Urgent outreach needed
- **81-100**: Critical - Immediate intervention required

### Risk Factors Explained

**no_creation_critical**: No events created beyond critical threshold for their pattern
- Annual accounts: >365 days
- Seasonal accounts: >180 days  
- Regular accounts: >90 days

**severe_revenue_decline**: Lost 75%+ of revenue vs previous period

**events_not_selling**: Creating events but no ticket sales for 60+ days

**major_tier_drop**: Dropped 2+ tiers (e.g., Key Account → Tier 3)

**bottom_quartile_revenue**: In bottom 25% of all accounts by revenue

## Interpreting Account Patterns

### Annual Accounts
- Run 1-2 major events per year
- Often create events 3-6 months in advance
- Risk escalates if no creation by expected time

### Seasonal Accounts  
- Run events in specific seasons (2-4 times/year)
- Create events 1-3 months ahead
- Check quarterly for missing seasons

### Regular Accounts
- Continuous events throughout year
- Short lead times (2-6 weeks)
- Risk escalates quickly after 45 days

### Struggling Accounts
- Creating events but zero sales
- High risk of complete churn
- Need immediate support

## Using the At-Risk Report

The system emails a prioritized list of at-risk accounts with:
- Risk score and level
- Current vs previous tier
- Revenue change %
- Days since last activity
- Specific risk factors

### Recommended Actions by Risk Level

**Critical (81-100)**
- Immediate phone call
- Discuss challenges and support options
- Offer training or platform assistance

**High (61-80)**
- Personalized email within 48 hours
- Schedule check-in call
- Review their event performance

**Moderate (41-60)**
- Include in retention campaigns
- Monitor for score changes
- Quarterly business review

## Monitoring and Validation

### Weekly Monitoring
Run the report weekly to catch:
- Regular accounts exceeding thresholds
- Seasonal accounts missing creation windows
- Sudden revenue drops

### Validation Checks
```python
# Check score distribution
Mean should be 25-40 (most accounts healthy)
Standard deviation >15 (good spread)

# Verify risk factors
At-risk accounts should have clear reasons
Known churned accounts should score >60
```

### Success Metrics
- Identify 80%+ of churns before they stop all activity
- Provide 3-6 month early warning for annual events
- Reduce false positives for seasonal businesses

## Troubleshooting

### All Scores Are 50
- Check if Accounts report loaded successfully
- Verify FirstEventCreated/LastEventCreated fields exist
- Ensure booking data has EventDate column

### Missing Event Creation Dates
- Accounts without LastEventCreated get fallback scoring
- Based on visible booking patterns only
- Less accurate but still functional

### Industry Lookups Failing
- Verify Accounts report has Industry/SubIndustry columns
- Check AccountId mapping between reports
- System continues without industry segmentation

## Advanced Features

### Seasonality Detection
The model includes sophisticated seasonality detection that:
- Analyzes when events are typically created (not held)
- Adjusts risk during known quiet periods
- Increases risk if missing expected creation windows

### Free Event Detection
Identifies accounts only running free events by checking:
- Total fees (BookingFee + CardFee + ProcessingFee + TicketFee) = 0
- Revenue = 0
- Flags as struggling business model

### Velocity Tracking
For Regular/Seasonal patterns:
- Compares recent 90-day activity to historical rate
- Flags significant slowdowns
- Early indicator of business challenges

## Best Practices

1. **Run Weekly**: Optimal frequency for all account types
2. **Act on Critical Risks**: Contact within 24-48 hours
3. **Track Interventions**: Note which accounts were contacted
4. **Monitor Trends**: Watch for increasing risk scores
5. **Validate Regularly**: Check model accuracy quarterly

## Integration with Business Processes

### Sales Team Actions
1. Filter CRM by Churn_Risk > 60
2. Sort by revenue impact
3. Assign accounts for immediate outreach
4. Track intervention success

### Customer Success Workflows  
1. Set up risk score alerts in Zoho
2. Create automated email campaigns by risk tier
3. Schedule business reviews for moderate risks
4. Prioritize support for critical accounts

### Executive Reporting
- Total revenue at risk by tier
- Churn prediction accuracy trends
- Intervention success rates
- Pattern distribution changes

## Conclusion

The enhanced churn risk model provides unprecedented visibility into account health by tracking event creation patterns. By acting on these early warning signals, you can dramatically improve retention rates and protect revenue from at-risk accounts.