# Documentation Updates Summary

This document summarizes the updates made to the documentation to reflect the current implementation as of November 2024.

## retention_priority_field_guide.md

### Major Updates:
1. **Revenue Analysis Section**: Completely rewritten to reflect two types of revenue analysis:
   - Strategic Revenue Drops (Year-over-Year comparisons)
   - Rapid Revenue Drops (4-week trends for high-value accounts only)

2. **Education Accounts**: Updated with specific holiday periods:
   - Scottish schools: Mid-June to mid-August
   - English/Welsh schools: July to early September
   - Added postcode-based detection for Scottish schools

3. **Annual Events**: Clarified the 1-2 month window for priority boosting

4. **Priority Scoring Details**: Added new section with:
   - Complete scoring formula
   - Tier weights and severity scores
   - Threshold values for each priority level
   - Maximum score cap of 20

## activity_rating_field_guide.md

### Major Updates:
1. **At Risk**: Updated new account threshold from 14-27 days to 15-28 days

2. **Churned**: Added condition that Regular/Continuous accounts need minimal activity (<10 tickets or <£100 revenue) to be marked as churned, preventing false positives for accounts still selling tickets

3. **Returned**: Clarified that only accounts with Years_Loyalty > 1 can be marked as Returned

4. **Education Accounts**: Replaced generic summer handling with specific pattern detection:
   - Term-time pattern: 60%+ activity September-June
   - Summer programs: Any July/August activity

5. **Scottish Schools**: Added specific postcode prefixes for detection

6. **Tier-Based Features**: Expanded section to include:
   - Outreach rating details
   - Tier loss detection and immediate churn marking

7. **Data Sources**: Added new factors considered:
   - Current activity levels (tickets and revenue)
   - Revenue drops
   - Postcode for Scottish school detection

## event_frequency_logic.md

No changes needed - this documentation remains accurate.

## revenue_factor_implementation.md

### Major Updates:
1. **Overview**: Clarified this is for strategic analysis, not rapid drop detection

2. **Function Signatures**: Updated all function signatures to match current implementation:
   - Removed SubIndustry references
   - Updated parameter names and types
   - Corrected return value structures

3. **Lifecycle Stages**: Replaced education seasonality function with lifecycle stage calculation

4. **Scoring Logic**: Updated to reflect actual percentage thresholds:
   - Severe: <25% (not <30%)
   - Adjusted quintile drop scoring

5. **Education Handling**: Removed special education handling as it's done in retention priority module

6. **Usage Example**: Updated with actual parameter names and return structure

## Key Implementation Insights

1. **Performance Optimization**: The current implementation heavily uses vectorized pandas operations for performance, processing thousands of accounts in seconds.

2. **Separation of Concerns**: 
   - Strategic revenue analysis (revenue_factor.py)
   - Rapid drop detection (account_processor.py)
   - Priority calculation (retention_priority.py)

3. **Data Quality Handling**: Extensive validation and fallback logic throughout to handle missing or invalid data gracefully.

4. **Business Logic Complexity**: The system handles numerous edge cases including new accounts, tier changes, education holidays, and seasonal patterns.