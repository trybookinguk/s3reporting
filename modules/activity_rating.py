"""
Hybrid activity rating calculation for TryBooking accounts.
Combines AU rating categories with UK pattern-aware logic for enhanced accuracy.

Rating Values (10 total):
1. Unactivated - AccountStatus = Unactivated
2. Never Logged In - Activated but LastLogIn is blank
3. Never Transacted - Logged in but zero bookings ever
4. Active Paid - Recent login + paid revenue in last 180 days
5. Active Free - Recent login + bookings but £0 payment value
6. Outreach - High-tier annual/seasonal approaching expected window (UK enhancement)
7. At Risk - No recent activity (with UK's pattern-aware thresholds)
8. Churned - Last login >12 months (or automatic detection)
9. Re-Activated - Was churned/inactive but now active again
10. Suspended or Closed - AccountStatus = Suspended/Closed
"""
import pandas as pd
import logging

logger = logging.getLogger(__name__)

# Constants for rating thresholds (in days)
RECENT_LOGIN_DAYS = 180  # 6 months
CHURNED_LOGIN_DAYS = 365  # 12 months

# Activity thresholds
MIN_TICKETS_FOR_MEANINGFUL = 10
MIN_REVENUE_FOR_MEANINGFUL = 100


def calculate_activity_ratings(df):
    """
    Hybrid activity rating calculation for entire DataFrame.

    Combines AU's simplified categories with UK's pattern-aware logic.

    Args:
        df: DataFrame with columns:
            Required:
            - Event_Frequency_Current, Event_Frequency_Previous
            - days_since_last, has_historical
            - tickets_current, revenue_current
            - account_created_date
            Optional (for enhanced logic):
            - LastLogIn, AccountStatus
            - Current_Tier, Previous_Tier
            - last_event_date, avg_lead_days
            - months_active_historical, Industry, Postcode

    Returns:
        pd.Series: Activity ratings for all accounts
    """
    # Initialize result series - default to 'Active Paid'
    ratings = pd.Series('Active Paid', index=df.index)

    today = pd.Timestamp.now().date()
    today_ts = pd.Timestamp(today)

    # ========================================================================
    # STEP 1: Account Status Checks (AU categories)
    # These take absolute priority - if account is suspended/closed, nothing else matters
    # ========================================================================

    has_account_status = 'AccountStatus' in df.columns

    if has_account_status:
        # 1. Suspended or Closed
        suspended_closed = df['AccountStatus'].isin(['Suspended', 'Closed'])
        ratings[suspended_closed] = 'Suspended or Closed'
        logger.debug(f"Suspended/Closed: {suspended_closed.sum()}")

        # 2. Unactivated
        unactivated = df['AccountStatus'] == 'Unactivated'
        ratings[unactivated] = 'Unactivated'
        logger.debug(f"Unactivated: {unactivated.sum()}")
    else:
        suspended_closed = pd.Series(False, index=df.index)
        unactivated = pd.Series(False, index=df.index)

    # Mask for accounts that need further classification
    needs_classification = ~(suspended_closed | unactivated)

    # ========================================================================
    # STEP 2: Login-based classifications (AU categories)
    # ========================================================================

    has_last_login = 'LastLogIn' in df.columns

    if has_last_login:
        # Parse LastLogIn dates
        last_login_dt = pd.to_datetime(df['LastLogIn'], errors='coerce')

        # Handle timezone if present
        if last_login_dt.dt.tz is not None:
            today_ts = pd.Timestamp(today, tz='UTC')

        days_since_login = (today_ts - last_login_dt).dt.days
        has_logged_in = last_login_dt.notna()
        recent_login = has_logged_in & (days_since_login <= RECENT_LOGIN_DAYS)
        very_old_login = has_logged_in & (days_since_login > CHURNED_LOGIN_DAYS)

        # 3. Never Logged In - Activated but no login
        never_logged_in = needs_classification & ~has_logged_in
        ratings[never_logged_in] = 'Never Logged In'
        logger.debug(f"Never Logged In: {never_logged_in.sum()}")
    else:
        # If no LastLogIn field, infer from activity
        has_logged_in = pd.Series(True, index=df.index)  # Assume logged in
        recent_login = pd.Series(True, index=df.index)   # Assume recent
        very_old_login = pd.Series(False, index=df.index)
        never_logged_in = pd.Series(False, index=df.index)

    # ========================================================================
    # STEP 3: Transaction-based classifications
    # ========================================================================

    # Get booking counts - check both lifetime and current
    has_bookings_ever = pd.Series(True, index=df.index)  # Default to true

    if 'tickets_lifetime' in df.columns:
        has_bookings_ever = df['tickets_lifetime'] > 0
    elif 'tickets_current' in df.columns and 'tickets_prev' in df.columns:
        has_bookings_ever = (df['tickets_current'] > 0) | (df['tickets_prev'] > 0)

    # 4. Never Transacted - Logged in but no bookings ever
    never_transacted = (
        needs_classification &
        has_logged_in &
        ~has_bookings_ever &
        ~never_logged_in
    )
    ratings[never_transacted] = 'Never Transacted'
    logger.debug(f"Never Transacted: {never_transacted.sum()}")

    # ========================================================================
    # STEP 4: Activity-based classifications (6-month window)
    # ========================================================================

    # Get current period metrics
    tickets_current = df['tickets_current'].fillna(0) if 'tickets_current' in df.columns else pd.Series(0, index=df.index)
    revenue_current = df['revenue_current'].fillna(0) if 'revenue_current' in df.columns else pd.Series(0, index=df.index)

    has_recent_bookings = tickets_current > 0
    has_recent_paid = revenue_current > 0

    # Accounts that have transacted and need activity classification
    active_accounts = needs_classification & has_bookings_ever & ~never_logged_in & ~never_transacted

    # 5. Active Paid - Recent login + paid revenue in last 180 days
    active_paid = active_accounts & recent_login & has_recent_paid
    ratings[active_paid] = 'Active Paid'
    logger.debug(f"Active Paid: {active_paid.sum()}")

    # 6. Active Free - Recent login + bookings but £0 payment value
    active_free = active_accounts & recent_login & has_recent_bookings & ~has_recent_paid
    ratings[active_free] = 'Active Free'
    logger.debug(f"Active Free: {active_free.sum()}")

    # ========================================================================
    # STEP 5: Churned detection (12+ months no login)
    # ========================================================================

    # Base churned on login if available
    if has_last_login:
        churned_by_login = active_accounts & very_old_login
        ratings[churned_by_login] = 'Churned'
        logger.debug(f"Churned (by login): {churned_by_login.sum()}")

    # ========================================================================
    # STEP 6: UK ENHANCEMENTS - Pattern-aware At Risk and Outreach
    # ========================================================================

    # Accounts that aren't already classified as Churned, Active Paid, or Active Free
    needs_risk_check = (
        active_accounts &
        ~recent_login &
        (ratings != 'Churned') &
        (ratings != 'Active Paid') &
        (ratings != 'Active Free')
    )

    # Get days since last booking
    days_since_last = df['days_since_last'].fillna(999) if 'days_since_last' in df.columns else pd.Series(999, index=df.index)

    # Check for meaningful current activity
    has_meaningful_activity = (tickets_current >= MIN_TICKETS_FOR_MEANINGFUL) | (revenue_current >= MIN_REVENUE_FOR_MEANINGFUL)
    has_minimal_activity = ~has_meaningful_activity

    # ========================================================================
    # STEP 6a: High-tier Annual/Seasonal Outreach (UK enhancement)
    # ========================================================================

    if all(col in df.columns for col in ['Current_Tier', 'Event_Frequency_Previous', 'last_event_date']):
        high_tier_mask = df['Current_Tier'].isin(['Key Account', 'High Value', 'Tier 4', 'Tier 3'])
        annual_seasonal_mask = df['Event_Frequency_Previous'].isin(['Annual', 'Seasonal'])
        has_last_event = df['last_event_date'].notna()
        currently_inactive = df['Event_Frequency_Current'] == 'Inactive'

        high_tier_annual_seasonal = (
            needs_risk_check &
            high_tier_mask &
            annual_seasonal_mask &
            has_last_event &
            currently_inactive
        )

        if high_tier_annual_seasonal.any():
            subset = df[high_tier_annual_seasonal].copy()

            # Convert last_event_date to pandas datetime for calculations
            last_event_ts = pd.to_datetime(subset['last_event_date'])

            # Calculate expected dates
            avg_leads = subset['avg_lead_days'].fillna(60) if 'avg_lead_days' in subset.columns else pd.Series(60, index=subset.index)
            expected_next_with_grace = (last_event_ts + pd.Timedelta(days=365+30)).dt.date
            expected_sale_start_ts = (last_event_ts + pd.Timedelta(days=365) - pd.to_timedelta(avg_leads, unit='D'))
            expected_sale_start = expected_sale_start_ts.dt.date
            outreach_date = (expected_sale_start_ts - pd.Timedelta(days=60)).dt.date

            # Apply conditions
            churned_high_tier = today >= expected_next_with_grace
            at_risk_high_tier = (~churned_high_tier) & (today >= expected_sale_start)
            outreach_high_tier = (~churned_high_tier) & (~at_risk_high_tier) & (today >= outreach_date)

            # Update ratings
            high_tier_indices = subset.index
            ratings[high_tier_indices[churned_high_tier]] = 'Churned'
            ratings[high_tier_indices[at_risk_high_tier]] = 'At Risk'
            ratings[high_tier_indices[outreach_high_tier]] = 'Outreach'

            logger.debug(f"High-tier Outreach: {outreach_high_tier.sum()}, At Risk: {at_risk_high_tier.sum()}, Churned: {churned_high_tier.sum()}")

    # ========================================================================
    # STEP 6b: Regular/Continuous pattern-aware thresholds (UK enhancement)
    # ========================================================================

    if 'Event_Frequency_Previous' in df.columns:
        # Identify accounts by pattern type
        is_continuous = df['Event_Frequency_Previous'] == 'Continuous'
        is_regular = df['Event_Frequency_Previous'] == 'Regular'
        is_annual_seasonal = df['Event_Frequency_Previous'].isin(['Annual', 'Seasonal'])

        # Continuous accounts: tighter thresholds (90 days churned, 30 days at risk)
        continuous_needs_check = needs_risk_check & is_continuous & has_minimal_activity
        continuous_churned = continuous_needs_check & (days_since_last >= 90)
        continuous_at_risk = continuous_needs_check & (days_since_last >= 30) & ~continuous_churned

        ratings[continuous_churned] = 'Churned'
        ratings[continuous_at_risk] = 'At Risk'
        logger.debug(f"Continuous - Churned: {continuous_churned.sum()}, At Risk: {continuous_at_risk.sum()}")

        # Regular accounts: medium thresholds (180 days churned, 90 days at risk)
        regular_needs_check = needs_risk_check & is_regular & has_minimal_activity
        regular_churned = regular_needs_check & (days_since_last >= 180)
        regular_at_risk = regular_needs_check & (days_since_last >= 90) & ~regular_churned

        ratings[regular_churned] = 'Churned'
        ratings[regular_at_risk] = 'At Risk'
        logger.debug(f"Regular - Churned: {regular_churned.sum()}, At Risk: {regular_at_risk.sum()}")

        # Annual/Seasonal: wider thresholds (handled above for high-tier, here for others)
        other_annual_seasonal = needs_risk_check & is_annual_seasonal & has_minimal_activity
        other_annual_seasonal = other_annual_seasonal & (ratings != 'Outreach') & (ratings != 'At Risk') & (ratings != 'Churned')

        annual_churned = other_annual_seasonal & (days_since_last >= 400)  # 365 + grace
        annual_at_risk = other_annual_seasonal & (days_since_last >= 300) & ~annual_churned

        ratings[annual_churned] = 'Churned'
        ratings[annual_at_risk] = 'At Risk'
        logger.debug(f"Annual/Seasonal - Churned: {annual_churned.sum()}, At Risk: {annual_at_risk.sum()}")

    # ========================================================================
    # STEP 6c: Default At Risk for remaining inactive accounts
    # ========================================================================

    # Any account still needing classification with no recent activity
    remaining_inactive = (
        needs_risk_check &
        (ratings == 'Active Paid') &  # Still has default rating
        ~has_recent_bookings &
        has_minimal_activity
    )

    # Apply default thresholds for accounts without pattern data
    default_churned = remaining_inactive & (days_since_last >= 365)
    default_at_risk = remaining_inactive & (days_since_last >= 180) & ~default_churned

    ratings[default_churned] = 'Churned'
    ratings[default_at_risk] = 'At Risk'
    logger.debug(f"Default - Churned: {default_churned.sum()}, At Risk: {default_at_risk.sum()}")

    # ========================================================================
    # STEP 7: Re-Activated detection
    # ========================================================================

    if 'Event_Frequency_Previous' in df.columns and 'Years_Loyalty' in df.columns:
        # Re-Activated: Was previously inactive but now has activity
        was_previously_inactive = df['Event_Frequency_Previous'] == 'Inactive'
        currently_active = df['Event_Frequency_Current'] != 'Inactive'
        existed_before = df['Years_Loyalty'] > 1

        reactivated = (
            needs_classification &
            was_previously_inactive &
            currently_active &
            existed_before &
            has_recent_bookings
        )
        ratings[reactivated] = 'Re-Activated'
        logger.debug(f"Re-Activated: {reactivated.sum()}")

    # ========================================================================
    # STEP 8: Tier loss and severe drop detection (UK enhancement)
    # ========================================================================

    if 'Current_Tier' in df.columns and 'Previous_Tier' in df.columns:
        has_lost_tier = (
            (df['Current_Tier'].isna() | (df['Current_Tier'] == '') | (df['Current_Tier'] == 'NIL')) &
            (df['Previous_Tier'].notna()) &
            (df['Previous_Tier'] != '') &
            (df['Previous_Tier'] != 'NIL')
        )

        # Tier loss with inactivity = Churned
        tier_loss_inactive = has_lost_tier & (df['Event_Frequency_Current'] == 'Inactive')
        ratings[tier_loss_inactive] = 'Churned'

        # Tier loss but still some activity = At Risk
        tier_loss_active = has_lost_tier & (df['Event_Frequency_Current'] != 'Inactive') & (ratings != 'Churned')
        ratings[tier_loss_active] = 'At Risk'

        logger.debug(f"Tier loss - Churned: {tier_loss_inactive.sum()}, At Risk: {tier_loss_active.sum()}")

    # Check for severe revenue drops
    if 'revenue_drop_score' in df.columns and 'revenue_prev' in df.columns:
        had_meaningful_revenue = df['revenue_prev'] >= MIN_REVENUE_FOR_MEANINGFUL
        severe_drop_string = df['revenue_drop_score'] == 'Severe'
        severe_drop_numeric = pd.to_numeric(df['revenue_drop_score'], errors='coerce') >= 3
        has_severe_drop = (severe_drop_string | severe_drop_numeric) & had_meaningful_revenue

        # Severe drop without already being Churned = At Risk
        at_risk_from_drop = has_severe_drop & (ratings != 'Churned') & (ratings != 'Suspended or Closed')
        ratings[at_risk_from_drop] = 'At Risk'
        logger.debug(f"Severe drop At Risk: {at_risk_from_drop.sum()}")

    # ========================================================================
    # STEP 9: Safety net - override incorrect classifications
    # ========================================================================

    # Accounts marked Churned but with significant current activity should be Active
    incorrectly_churned = (
        (ratings == 'Churned') &
        has_meaningful_activity
    )

    if incorrectly_churned.any():
        logger.warning(f"Safety net: {incorrectly_churned.sum()} accounts marked Churned despite significant activity")

        # Determine if Active Paid or Active Free
        has_paid = revenue_current > 0
        ratings[incorrectly_churned & has_paid] = 'Active Paid'
        ratings[incorrectly_churned & ~has_paid] = 'Active Free'

        # If revenue dropped significantly, mark as At Risk instead
        if 'revenue_prev' in df.columns:
            prev_revenue = df['revenue_prev'].fillna(0)
            revenue_dropped = (prev_revenue > MIN_REVENUE_FOR_MEANINGFUL) & (revenue_current < prev_revenue * 0.5)
            ratings[incorrectly_churned & revenue_dropped] = 'At Risk'

    # Log final distribution
    rating_counts = ratings.value_counts()
    logger.info(f"Rating distribution: {rating_counts.to_dict()}")

    return ratings


def is_education_pattern(months_list):
    """Detect education pattern from months active."""
    if not months_list or not isinstance(months_list, list):
        return False

    # Education pattern: active during term time, minimal summer activity
    term_months = set(range(9, 13)) | set(range(1, 7))  # Sept-Dec, Jan-June
    summer_months = {7, 8}  # July, August

    months_set = set(months_list)
    term_activity = len(months_set & term_months)
    summer_activity = len(months_set & summer_months)

    return term_activity >= 4 and summer_activity <= 1


def is_scottish_postcode(postcode):
    """Check if postcode is Scottish."""
    if not postcode or not isinstance(postcode, str):
        return False

    scottish_areas = {
        'AB', 'DD', 'DG', 'EH', 'FK', 'G', 'HS', 'IV', 'KA',
        'KW', 'KY', 'ML', 'PA', 'PH', 'TD', 'ZE'
    }

    # Extract letter portion
    area = postcode.strip().upper()
    letter_portion = ''.join(char for char in area if char.isalpha())[:2]

    return letter_portion in scottish_areas
