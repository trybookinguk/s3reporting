"""
MBR Chart Generation Module

Generates ready-to-paste PNG charts for the Monthly Business Review PPTX.
Each function corresponds to one or more slides in the deck.

Font handling: Uses bundled Poppins TTF files from the fonts/ directory,
so charts render correctly in CI (GitHub Actions) without system fonts.
"""

import os
import glob as glob_module

import matplotlib
matplotlib.use('Agg')  # Non-interactive backend for headless environments

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
from matplotlib import font_manager

# ---------------------------------------------------------------------------
# Font registration
# ---------------------------------------------------------------------------

_FONT_DIR = os.path.join(os.path.dirname(__file__), '..', 'fonts')
_FONTS_LOADED = False


def _load_poppins():
    """Register bundled Poppins fonts and set as default family."""
    global _FONTS_LOADED
    if _FONTS_LOADED:
        return

    for ttf in ['Poppins-Regular.ttf', 'Poppins-Bold.ttf', 'Poppins-SemiBold.ttf']:
        path = os.path.join(_FONT_DIR, ttf)
        if os.path.exists(path):
            font_manager.fontManager.addfont(path)

    plt.rcParams['font.family'] = 'Poppins'
    plt.rcParams['axes.unicode_minus'] = False
    _FONTS_LOADED = True


# ---------------------------------------------------------------------------
# Brand colour palette
# ---------------------------------------------------------------------------

INDUSTRY_COLOURS = {
    'Education': '#2E86AB',
    'Business': '#1B3A5C',
    'Sporting Club': '#4CAF50',
    'Community': '#F18F01',
    'Theatres': '#9C27B0',
    'Music': '#E91E63',
    'Charity': '#00BCD4',
    'Tourism': '#FF5722',
    'Festival': '#FFC107',
    'Dance': '#8BC34A',
    'Agriculture': '#795548',
    'Council / Gov': '#607D8B',
    'Associations': '#3F51B5',
    'Event Organiser': '#CDDC39',
    'Non Profit': '#009688',
}

# Fallback colours for industries not in the palette
_EXTRA_COLOURS = [
    '#FF6F61', '#6B5B95', '#88B04B', '#F7CAC9', '#92A8D1',
    '#955251', '#B565A7', '#009B77', '#DD4124', '#45B8AC',
]

BRAND_DARK = '#1B3A5C'
BRAND_ACCENT = '#2E86AB'
BRAND_LIGHT_BG = '#F8F9FA'
BRAND_GREEN = '#4CAF50'
BRAND_RED = '#E53935'

TIER_COLOURS = {
    'Key Account': '#1B3A5C',
    'High Value': '#2E86AB',
    'Tier 4': '#4CAF50',
    'Tier 3': '#F18F01',
    'Tier 2': '#FF5722',
    'Tier 1': '#9E9E9E',
    'Tier 2/1': '#9E9E9E',
    'Untiered': '#BDBDBD',
    'NIL': '#E0E0E0',
}


def _industry_colour(industry):
    """Return a consistent colour for an industry, falling back to extras."""
    if industry in INDUSTRY_COLOURS:
        return INDUSTRY_COLOURS[industry]
    # Deterministic fallback based on hash
    idx = hash(str(industry)) % len(_EXTRA_COLOURS)
    return _EXTRA_COLOURS[idx]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _save(fig, path, dpi=150):
    """Save figure with tight bounding box and close."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fig.savefig(path, dpi=dpi, bbox_inches='tight', facecolor='white', edgecolor='none')
    plt.close(fig)


def _fmt_currency(val):
    """Format a number as currency string (GBP)."""
    if pd.isna(val) or val is None:
        return ''
    if abs(val) >= 1_000_000:
        return f'\u00a3{val / 1_000_000:.1f}M'
    if abs(val) >= 1_000:
        return f'\u00a3{val / 1_000:.1f}K'
    return f'\u00a3{val:,.0f}'


def _fmt_number(val):
    """Format a number with commas."""
    if pd.isna(val) or val is None:
        return ''
    try:
        return f'{int(val):,}'
    except (ValueError, TypeError):
        return str(val)


def _fmt_pct(val):
    """Format as percentage."""
    if pd.isna(val) or val is None:
        return ''
    return f'{val:.1f}%'


def _brand_table(df, title, output_path, col_widths=None, fmt_map=None):
    """
    Render a DataFrame as a styled table image.

    Args:
        df: DataFrame to render
        title: Title displayed above the table
        output_path: Where to save the PNG
        col_widths: Optional list of relative column widths
        fmt_map: Optional dict mapping column names to format functions
    """
    _load_poppins()

    n_rows, n_cols = df.shape
    fig_width = max(10, n_cols * 1.5)
    fig_height = max(3, (n_rows + 2) * 0.45)

    fig, ax = plt.subplots(figsize=(fig_width, fig_height))
    ax.axis('off')
    ax.set_title(title, fontsize=14, fontweight='bold', pad=20, loc='left', color=BRAND_DARK)

    # Format cell text
    cell_text = []
    for _, row in df.iterrows():
        formatted_row = []
        for col in df.columns:
            val = row[col]
            if fmt_map and col in fmt_map:
                formatted_row.append(fmt_map[col](val))
            elif isinstance(val, float):
                if abs(val) >= 1000:
                    formatted_row.append(f'{val:,.0f}')
                else:
                    formatted_row.append(f'{val:.1f}')
            else:
                formatted_row.append(str(val) if not pd.isna(val) else '')
        cell_text.append(formatted_row)

    table = ax.table(
        cellText=cell_text,
        colLabels=df.columns.tolist(),
        loc='center',
        cellLoc='center',
    )
    table.auto_set_font_size(False)
    table.set_fontsize(9)

    if col_widths:
        for i, w in enumerate(col_widths):
            for row_idx in range(n_rows + 1):
                table[row_idx, i].set_width(w)

    # Style header row
    for j in range(n_cols):
        cell = table[0, j]
        cell.set_facecolor(BRAND_DARK)
        cell.set_text_props(color='white', fontweight='bold')
        cell.set_edgecolor('white')

    # Alternating row shading
    for i in range(1, n_rows + 1):
        colour = BRAND_LIGHT_BG if i % 2 == 0 else 'white'
        for j in range(n_cols):
            cell = table[i, j]
            cell.set_facecolor(colour)
            cell.set_edgecolor('#E0E0E0')

    table.scale(1, 1.6)
    _save(fig, output_path)


def _brand_pie(ax, labels, values, title, colours=None, legend_position='right'):
    """
    Draw a branded pie chart on the given axes.

    Args:
        ax: Matplotlib axes
        labels: List of segment labels
        values: List of numeric values
        title: Chart title
        colours: Optional list of colours (auto-mapped from INDUSTRY_COLOURS if None)
        legend_position: 'right' (default) or 'bottom'
    """
    if colours is None:
        colours = [_industry_colour(label) for label in labels]

    # Filter out zero/negative values
    mask = [v > 0 for v in values]
    labels = [l for l, m in zip(labels, mask) if m]
    values = [v for v, m in zip(values, mask) if m]
    colours = [c for c, m in zip(colours, mask) if m]

    # Sort largest to smallest
    if values:
        sorted_idx = sorted(range(len(values)), key=lambda i: values[i], reverse=True)
        labels = [labels[i] for i in sorted_idx]
        values = [values[i] for i in sorted_idx]
        colours = [colours[i] for i in sorted_idx]

    if not values:
        ax.text(0.5, 0.5, 'No data', ha='center', va='center', fontsize=12, color='#999')
        ax.set_title(title, fontsize=12, fontweight='bold', color=BRAND_DARK)
        return

    # Only show percentage labels for slices >= 3%
    total = sum(values)

    def autopct_func(pct):
        return f'{pct:.0f}%' if pct >= 3 else ''

    wedges, texts, autotexts = ax.pie(
        values, labels=None, autopct=autopct_func, colors=colours,
        startangle=90, pctdistance=0.75,
        wedgeprops={'edgecolor': 'white', 'linewidth': 1.5}
    )

    for txt in autotexts:
        txt.set_fontsize(8)
        txt.set_color('white')
        txt.set_fontweight('bold')

    # Add legend with values
    legend_labels = [
        f'{label} ({_fmt_currency(val)})' for label, val in zip(labels, values)
    ]
    if legend_position == 'bottom':
        ax.legend(wedges, legend_labels, loc='upper center', bbox_to_anchor=(0.5, -0.05),
                  ncol=3, fontsize=7, frameon=False)
    else:
        ax.legend(wedges, legend_labels, loc='center left', bbox_to_anchor=(1.0, 0.5),
                  fontsize=7, frameon=False)

    ax.set_title(title, fontsize=12, fontweight='bold', color=BRAND_DARK, pad=10)


def _brand_bar(ax, categories, values, title, colour=BRAND_ACCENT, horizontal=False,
               fmt_func=None, ylabel=None):
    """
    Draw a branded bar/column chart.

    Args:
        ax: Matplotlib axes
        categories: Labels for each bar
        values: Numeric values
        title: Chart title
        colour: Bar colour (single or list)
        horizontal: If True, draw horizontal bars
        fmt_func: Optional function to format bar value labels
        ylabel: Optional Y-axis label
    """
    positions = range(len(categories))

    if isinstance(colour, list):
        bar_colours = colour
    else:
        bar_colours = [colour] * len(categories)

    if horizontal:
        bars = ax.barh(positions, values, color=bar_colours, edgecolor='white', height=0.6)
        ax.set_yticks(positions)
        ax.set_yticklabels(categories, fontsize=9)
        ax.invert_yaxis()
        if fmt_func:
            for bar, val in zip(bars, values):
                ax.text(bar.get_width() + max(values) * 0.01, bar.get_y() + bar.get_height() / 2,
                        fmt_func(val), va='center', fontsize=8, color=BRAND_DARK)
    else:
        bars = ax.bar(positions, values, color=bar_colours, edgecolor='white', width=0.6)
        ax.set_xticks(positions)
        ax.set_xticklabels(categories, fontsize=9, rotation=45, ha='right')
        if fmt_func:
            for bar, val in zip(bars, values):
                ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(),
                        fmt_func(val), ha='center', va='bottom', fontsize=7, color=BRAND_DARK)

    ax.set_title(title, fontsize=12, fontweight='bold', color=BRAND_DARK, pad=10)
    if ylabel:
        ax.set_ylabel(ylabel, fontsize=9)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.tick_params(axis='both', labelsize=8)


# ---------------------------------------------------------------------------
# Slide generators
# ---------------------------------------------------------------------------

def _slide03_targets(view_dir, charts_dir):
    """Slide 3 — New Account Targets table image."""
    outputs = []

    # Find planning model CSV
    csv_path = _find_csv(view_dir, '_planning_model.csv', folder='planning')
    if csv_path is None:
        csv_path = _find_csv(view_dir, '_targets.csv', folder='planning')
    if csv_path is None:
        return outputs

    _load_poppins()
    df = pd.read_csv(csv_path)

    # Select key columns for the table
    key_cols = [c for c in df.columns if c in [
        'Year', 'Month', 'Month Name',
        'Total New Accounts', 'Total Fees', 'Total Ticket Revenue', 'Total Tickets Sold',
        'Accounts Index %', 'Base Target', 'Stretch Target',
    ]]
    if not key_cols:
        key_cols = df.columns.tolist()[:10]

    display_df = df[key_cols].copy()

    out_path = os.path.join(charts_dir, 'slide03_targets.png')
    _brand_table(display_df, 'New Account Targets', out_path)
    outputs.append(out_path)
    return outputs


def _slide04_new_accounts(view_dir, charts_dir):
    """Slide 4 — New Account Dynamics: YoY line charts."""
    outputs = []

    csv_path = _find_csv(view_dir, '_summary.csv')
    if csv_path is None:
        csv_path = _find_csv(view_dir, '.csv')  # Main monthly metrics
    if csv_path is None:
        return outputs

    _load_poppins()
    df = pd.read_csv(csv_path)

    if 'Month' not in df.columns or 'Total New Accounts' not in df.columns:
        return outputs

    # Need to split by year for YoY
    if 'Year' in df.columns:
        years = sorted(df['Year'].unique())
    else:
        return outputs

    # Chart 1: New accounts by month, YoY line
    fig, ax = plt.subplots(figsize=(10, 5))

    for year in years:
        year_df = df[df['Year'] == year].sort_values('Month')
        ax.plot(year_df['Month'], year_df['Total New Accounts'],
                marker='o', linewidth=2, label=str(year))

    ax.set_title('New Accounts by Month — YoY Comparison', fontsize=13, fontweight='bold',
                 color=BRAND_DARK)
    ax.set_xlabel('Month', fontsize=10)
    ax.set_ylabel('New Accounts', fontsize=10)
    ax.set_xticks(range(1, 13))
    ax.set_xticklabels([_month_abbr(m) for m in range(1, 13)], fontsize=9)
    ax.legend(fontsize=9)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f'{int(x):,}'))

    out1 = os.path.join(charts_dir, 'slide04_new_accounts_yoy.png')
    _save(fig, out1)
    outputs.append(out1)

    # Chart 2: YoY growth percentage
    if 'YoY Total New Accounts %' in df.columns:
        fig2, ax2 = plt.subplots(figsize=(10, 5))
        latest_year = max(years)
        growth_df = df[df['Year'] == latest_year].sort_values('Month')
        colours = [BRAND_GREEN if v >= 0 else BRAND_RED
                   for v in growth_df['YoY Total New Accounts %'].fillna(0)]

        ax2.bar(growth_df['Month'], growth_df['YoY Total New Accounts %'].fillna(0),
                color=colours, edgecolor='white', width=0.6)
        ax2.axhline(y=0, color='#999', linewidth=0.8)
        ax2.set_title(f'New Accounts YoY Growth % ({latest_year} vs {latest_year - 1})',
                      fontsize=13, fontweight='bold', color=BRAND_DARK)
        ax2.set_xlabel('Month', fontsize=10)
        ax2.set_ylabel('YoY Growth %', fontsize=10)
        ax2.set_xticks(range(1, 13))
        ax2.set_xticklabels([_month_abbr(m) for m in range(1, 13)], fontsize=9)
        ax2.spines['top'].set_visible(False)
        ax2.spines['right'].set_visible(False)

        out2 = os.path.join(charts_dir, 'slide04_yoy_growth.png')
        _save(fig2, out2)
        outputs.append(out2)

    return outputs


def _slide05_performance(view_dir, charts_dir):
    """Slide 5 — Monthly performance YoY table image."""
    outputs = []

    csv_path = _find_csv(view_dir, '_monthly_performance_yoy_comparison.csv')
    if csv_path is None:
        return outputs

    _load_poppins()
    df = pd.read_csv(csv_path)

    out_path = os.path.join(charts_dir, 'slide05_performance.png')
    _brand_table(df, 'Monthly Performance — YoY Comparison', out_path)
    outputs.append(out_path)
    return outputs


def _slide06_revenue(view_dir, charts_dir):
    """Slide 6 — Revenue: summary table + 3-year fees bar chart with YoY lines."""
    outputs = []

    _load_poppins()

    # Revenue summary table (same month across years)
    rev_csv = _find_csv(view_dir, '_monthly_revenue_summary.csv')
    if rev_csv:
        df = pd.read_csv(rev_csv)
        out_table = os.path.join(charts_dir, 'slide06_revenue_table.png')
        _brand_table(df, 'Revenue Summary', out_table)
        outputs.append(out_table)

    # 3-year fees by month bar chart with YoY growth lines
    yoy_csv = _find_csv(view_dir, '_monthly_performance_yoy_comparison.csv')
    if yoy_csv:
        df = pd.read_csv(yoy_csv)

        # Find fee columns and YoY fee columns
        fee_cols = [c for c in df.columns if c.endswith(' Fees') and 'vs' not in c]
        yoy_cols = [c for c in df.columns if 'Fees %' in c]

        if len(fee_cols) >= 2:
            fig, ax1 = plt.subplots(figsize=(14, 6))

            months = df['Month Name'].str[:3].tolist()
            x = np.arange(len(months))
            n_years = len(fee_cols)
            bar_width = 0.7 / n_years

            bar_colours = [BRAND_DARK, BRAND_ACCENT, BRAND_GREEN][:n_years]

            # Draw bars for each year
            for i, col in enumerate(fee_cols):
                year_label = col.replace(' Fees', '')
                offset = (i - n_years / 2 + 0.5) * bar_width
                values = df[col].fillna(0).tolist()
                ax1.bar(x + offset, values, bar_width, label=f'{year_label} Fees',
                        color=bar_colours[i], zorder=3)

            ax1.set_xlabel('')
            ax1.set_ylabel('Fees (\u00a3)', fontsize=10)
            ax1.set_xticks(x)
            ax1.set_xticklabels(months, fontsize=9)
            ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, p: _fmt_currency(v)))
            ax1.grid(axis='y', alpha=0.3, zorder=0)

            # YoY lines on secondary axis
            if yoy_cols:
                ax2 = ax1.twinx()
                ax2.set_ylabel('YoY Growth %', fontsize=10)

                line_colours = ['#DAA520', '#E91E63'][:len(yoy_cols)]
                line_styles = ['-', '--'][:len(yoy_cols)]

                for i, col in enumerate(yoy_cols):
                    # Extract year pair label (e.g. "2025 vs 2024")
                    label = col.replace(' Fees %', '')
                    values = df[col].tolist()
                    # Only plot months with actual data (not -100% for future months)
                    valid_mask = [v is not None and not pd.isna(v) and v > -99 for v in values]
                    valid_x = [xi for xi, m in zip(x, valid_mask) if m]
                    valid_v = [v for v, m in zip(values, valid_mask) if m]
                    ax2.plot(valid_x, valid_v, color=line_colours[i],
                             linestyle=line_styles[i], marker='o', markersize=4,
                             linewidth=2, label=f'YoY {label} %', zorder=5)

                ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, p: f'{v:.0f}%'))

            # Combined legend
            lines1, labels1 = ax1.get_legend_handles_labels()
            if yoy_cols:
                lines2, labels2 = ax2.get_legend_handles_labels()
                ax1.legend(lines1 + lines2, labels1 + labels2,
                           loc='upper center', bbox_to_anchor=(0.5, -0.08),
                           ncol=n_years + len(yoy_cols), fontsize=8)
            else:
                ax1.legend(loc='upper center', bbox_to_anchor=(0.5, -0.08),
                           ncol=n_years, fontsize=8)

            ax1.set_title('Fees By Month (YoY Growth)', fontsize=13, fontweight='bold', pad=12)

            out_bar = os.path.join(charts_dir, 'slide06_fees_monthly.png')
            _save(fig, out_bar)
            outputs.append(out_bar)

    return outputs


def _slide08_all_industry(view_dir, charts_dir):
    """Slide 8 — All clients industry: CY vs PY fees pies."""
    outputs = []

    csv_path = _find_csv(view_dir, '_industry_by_tier.csv', folder='industry')
    if csv_path is None:
        return outputs

    _load_poppins()
    df = pd.read_csv(csv_path)
    cy_label, py_label = _read_period_labels(view_dir)

    # Filter to 'All Active' segment
    all_active = df[df['Segment'] == 'All Active'].copy()
    if all_active.empty:
        return outputs

    labels = all_active['Industry'].tolist()
    cy_fees = all_active['Fees'].tolist()

    # CY pie
    fig1, ax1 = plt.subplots(figsize=(9, 7))
    _brand_pie(ax1, labels, cy_fees, f'All Active — Fees ({cy_label})',
               legend_position='bottom')
    out1 = os.path.join(charts_dir, 'slide08_all_fees_cy.png')
    _save(fig1, out1)
    outputs.append(out1)

    # PY pie (if data available)
    if 'PY Fees' in all_active.columns:
        py_fees = all_active['PY Fees'].fillna(0).tolist()
        fig2, ax2 = plt.subplots(figsize=(9, 7))
        _brand_pie(ax2, labels, py_fees, f'All Active — Fees ({py_label})',
                   legend_position='bottom')
        out2 = os.path.join(charts_dir, 'slide08_all_fees_py.png')
        _save(fig2, out2)
        outputs.append(out2)

    return outputs


def _slide09_hv_industry(view_dir, charts_dir):
    """Slide 9 — Key+HV industry: 2x pie (CY vs PY fees)."""
    outputs = []

    csv_path = _find_csv(view_dir, '_industry_by_tier.csv', folder='industry')
    if csv_path is None:
        return outputs

    _load_poppins()
    df = pd.read_csv(csv_path)
    cy_label, py_label = _read_period_labels(view_dir)

    hv_df = df[df['Segment'] == 'Key Account + High Value'].copy()
    if hv_df.empty:
        return outputs

    labels = hv_df['Industry'].tolist()
    cy_fees = hv_df['Fees'].tolist()

    # CY pie
    fig1, ax1 = plt.subplots(figsize=(9, 7))
    _brand_pie(ax1, labels, cy_fees, f'Key + High Value — Fees ({cy_label})',
               legend_position='bottom')
    out1 = os.path.join(charts_dir, 'slide09_hv_fees_cy.png')
    _save(fig1, out1)
    outputs.append(out1)

    # PY pie (if data available)
    if 'PY Fees' in hv_df.columns:
        py_fees = hv_df['PY Fees'].fillna(0).tolist()
        fig2, ax2 = plt.subplots(figsize=(9, 7))
        _brand_pie(ax2, labels, py_fees, f'Key + High Value — Fees ({py_label})',
                   legend_position='bottom')
        out2 = os.path.join(charts_dir, 'slide09_hv_fees_py.png')
        _save(fig2, out2)
        outputs.append(out2)

    return outputs


def _slide10_new_industry(view_dir, charts_dir):
    """Slide 10 — New accounts industry: 2x pie (CY vs PY fees)."""
    outputs = []

    csv_path = _find_csv(view_dir, '_industry_by_tier.csv', folder='industry')
    if csv_path is None:
        return outputs

    _load_poppins()
    df = pd.read_csv(csv_path)
    cy_label, py_label = _read_period_labels(view_dir)

    new_df = df[df['Segment'] == 'New Accounts'].copy()
    if new_df.empty:
        return outputs

    labels = new_df['Industry'].tolist()
    cy_fees = new_df['Fees'].tolist()

    fig1, ax1 = plt.subplots(figsize=(9, 7))
    _brand_pie(ax1, labels, cy_fees, f'New Accounts (Last 3 Months) — Fees ({cy_label})',
               legend_position='bottom')
    out1 = os.path.join(charts_dir, 'slide10_new_fees_cy.png')
    _save(fig1, out1)
    outputs.append(out1)

    if 'PY Fees' in new_df.columns:
        py_fees = new_df['PY Fees'].fillna(0).tolist()
        fig2, ax2 = plt.subplots(figsize=(9, 7))
        _brand_pie(ax2, labels, py_fees, f'New Accounts (Last 3 Months) — Fees ({py_label})',
                   legend_position='bottom')
        out2 = os.path.join(charts_dir, 'slide10_new_fees_py.png')
        _save(fig2, out2)
        outputs.append(out2)

    return outputs


def _free_paid_chart(df, title, output_path):
    """Render a single free/paid 100% stacked bar chart."""
    if 'Industry' not in df.columns:
        if df.index.name == 'Industry':
            df = df.reset_index()
        else:
            return None

    if 'Free %' not in df.columns or 'Paid %' not in df.columns:
        return None

    # Filter out Ticket Purchaser
    df = df[~df['Industry'].str.contains('Ticket Purchas', case=False, na=False)].copy()

    if df.empty:
        return None

    # Sort by Paid % descending (highest paid percentage at top)
    df = df.sort_values('Paid %', ascending=True)

    n_industries = len(df)
    fig_height = max(5, n_industries * 0.45 + 1.5)
    fig, ax = plt.subplots(figsize=(10, fig_height))

    industries = df['Industry'].tolist()
    free_pct = df['Free %'].tolist()
    paid_pct = df['Paid %'].tolist()
    y_pos = range(len(industries))

    ax.barh(y_pos, paid_pct, color=BRAND_ACCENT, label='Paid', height=0.6)
    ax.barh(y_pos, free_pct, left=paid_pct, color='#E0E0E0', label='Free', height=0.6)

    ax.set_yticks(y_pos)
    ax.set_yticklabels(industries, fontsize=9)
    ax.set_xlabel('Percentage', fontsize=10)
    ax.set_title(title, fontsize=13, fontweight='bold', color=BRAND_DARK)
    ax.legend(fontsize=9)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.set_xlim(0, 100)

    _save(fig, output_path)
    return output_path


def _slide11_free_paid(view_dir, charts_dir):
    """Slide 11 — Free/Paid split: CY vs PY month + CY vs PY YTD."""
    outputs = []
    _load_poppins()

    # Read labels
    label_csv = _find_csv(view_dir, '_free_paid_labels.csv', folder='industry')
    labels = {'CY': 'Current Month', 'PY': 'Previous Year Month',
              'YTD_CY': 'YTD Current Year', 'YTD_PY': 'YTD Previous Year'}
    if label_csv:
        labels_df = pd.read_csv(label_csv)
        for _, row in labels_df.iterrows():
            labels[row['Period']] = row['Label']

    # Generate charts for each period
    chart_configs = [
        ('_free_vs_paid_cy.csv', labels['CY'], 'slide11_free_paid_cy.png'),
        ('_free_vs_paid_py.csv', labels['PY'], 'slide11_free_paid_py.png'),
        ('_free_vs_paid_ytd_cy.csv', labels['YTD_CY'], 'slide11_free_paid_ytd_cy.png'),
        ('_free_vs_paid_ytd_py.csv', labels['YTD_PY'], 'slide11_free_paid_ytd_py.png'),
    ]

    for suffix, label, filename in chart_configs:
        csv_path = _find_csv(view_dir, suffix, folder='industry')
        if csv_path:
            df = pd.read_csv(csv_path)
            out = _free_paid_chart(df, f'Free vs Paid Events by Industry \u2014 {label}',
                                   os.path.join(charts_dir, filename))
            if out:
                outputs.append(out)

    return outputs


def _slide12_avg_metrics_table(df, title, output_path):
    """Render avg transaction metrics as a table with currency to 2dp."""
    if 'Metric' not in df.columns:
        return None

    # Detect formatting by metric name keywords
    def _is_currency(metric):
        return any(kw in metric for kw in ['Price', 'Value'])

    def _is_pct(metric):
        return metric.startswith('%')

    # Pre-format values for display
    year_cols = [c for c in df.columns if c != 'Metric']
    formatted_df = df.copy()
    for _, row in formatted_df.iterrows():
        metric = row['Metric']
        for col in year_cols:
            val = row[col]
            if pd.isna(val):
                formatted_df.loc[formatted_df['Metric'] == metric, col] = ''
            elif _is_currency(metric):
                formatted_df.loc[formatted_df['Metric'] == metric, col] = f'\u00a3{val:,.2f}'
            elif _is_pct(metric):
                formatted_df.loc[formatted_df['Metric'] == metric, col] = f'{val:.1f}%'
            else:
                formatted_df.loc[formatted_df['Metric'] == metric, col] = f'{val:.2f}'

    _brand_table(formatted_df, title, output_path)
    return output_path


def _slide12_avg_metrics(view_dir, charts_dir):
    """Slide 12 — Average transaction metrics: month + YTD tables."""
    outputs = []
    _load_poppins()

    # Month-specific metrics
    month_csv = _find_csv(view_dir, '_monthly_avg_transaction_metrics.csv')
    if month_csv:
        df = pd.read_csv(month_csv)

        # Read period label for title
        cy_label, _ = _read_period_labels(view_dir)
        month_title = cy_label if cy_label != 'Current Year' else 'Monthly'

        out_table = os.path.join(charts_dir, 'slide12_avg_metrics_month.png')
        result = _slide12_avg_metrics_table(df, f'Transaction Metrics \u2014 {month_title}', out_table)
        if result:
            outputs.append(result)

    # YTD metrics
    ytd_csv = _find_csv(view_dir, '_ytd_avg_transaction_metrics.csv')
    if ytd_csv:
        df = pd.read_csv(ytd_csv)

        cy_label, _ = _read_period_labels(view_dir)
        # Extract month name from label (e.g. "January 2026" -> "January")
        month_part = cy_label.split()[0] if cy_label != 'Current Year' else ''
        ytd_title = f'YTD (Jan\u2013{month_part})' if month_part else 'YTD'

        out_table = os.path.join(charts_dir, 'slide12_avg_metrics_ytd.png')
        result = _slide12_avg_metrics_table(df, f'Transaction Metrics \u2014 {ytd_title}', out_table)
        if result:
            outputs.append(result)

    return outputs


def _slide14_retention(view_dir, charts_dir):
    """Slide 14 — Retention: tier concentration + expansion revenue charts."""
    outputs = []

    _load_poppins()

    # Tier concentration bar chart
    conc_csv = _find_csv(view_dir, '_tier_concentration.csv', folder='cohorts')
    if conc_csv:
        df = pd.read_csv(conc_csv)
        if 'Tier' in df.columns and 'Fees' in df.columns:
            fig, ax = plt.subplots(figsize=(10, 6))
            colours = [TIER_COLOURS.get(t, '#999') for t in df['Tier']]
            _brand_bar(ax, df['Tier'].tolist(), df['Fees'].tolist(),
                       'Fee Revenue by Tier', colour=colours, fmt_func=_fmt_currency,
                       ylabel='Fees (\u00a3)')
            out = os.path.join(charts_dir, 'slide14_tier_fees.png')
            _save(fig, out)
            outputs.append(out)

    # Revenue maturity comparison
    mat_csv = _find_csv(view_dir, '_revenue_maturity_yoy_comparison.csv', folder='cohorts')
    if mat_csv:
        df = pd.read_csv(mat_csv)
        if 'Account_Age' in df.columns:
            fig, ax = plt.subplots(figsize=(10, 6))

            age_bands = df['Account_Age'].tolist()
            x = np.arange(len(age_bands))
            width = 0.35

            if '2024_Revenue' in df.columns and '2025_Revenue' in df.columns:
                ax.bar(x - width / 2, df['2024_Revenue'], width, label='2024',
                       color='#90CAF9', edgecolor='white')
                ax.bar(x + width / 2, df['2025_Revenue'], width, label='2025',
                       color=BRAND_ACCENT, edgecolor='white')

                ax.set_xticks(x)
                ax.set_xticklabels(age_bands, fontsize=9)
                ax.set_title('Revenue by Account Maturity — YoY', fontsize=13,
                             fontweight='bold', color=BRAND_DARK)
                ax.set_ylabel('Revenue (\u00a3)', fontsize=10)
                ax.legend(fontsize=9)
                ax.spines['top'].set_visible(False)
                ax.spines['right'].set_visible(False)
                ax.yaxis.set_major_formatter(
                    mticker.FuncFormatter(lambda x, _: _fmt_currency(x)))

                out = os.path.join(charts_dir, 'slide14_retention.png')
                _save(fig, out)
                outputs.append(out)

    return outputs


def _draw_funnel_chart(stages, values, colours, title, output_path):
    """
    Draw a single wide trapezoid funnel chart and save it.

    Each stage is a centred trapezoid whose width is proportional to its
    value.  The count, percentage of the top-of-funnel, and stage label
    are rendered inside each shape.

    Args:
        stages:      List of stage labels (top to bottom).
        values:      List of numeric values per stage.
        colours:     List of fill colours per stage.
        title:       Chart title.
        output_path: Where to save the PNG.
    """
    from matplotlib.patches import Polygon

    _load_poppins()

    n = len(stages)
    row_h = 1.0
    gap = 0.15
    total_h = n * row_h + (n - 1) * gap
    funnel_half_w = 3.5   # half-width of widest stage
    top_value = values[0] if values[0] > 0 else 1

    fig, ax = plt.subplots(figsize=(10, max(4, total_h * 1.1 + 1.5)))
    ax.set_aspect('equal')
    ax.axis('off')

    ax.set_title(title, fontsize=14, fontweight='bold', color=BRAND_DARK,
                 pad=18, loc='center')

    x_c = 0.0

    for i, (stage, val, colour) in enumerate(zip(stages, values, colours)):
        y_top = i * (row_h + gap)
        y_bot = y_top + row_h

        half_w_top = (val / top_value) * funnel_half_w if top_value > 0 else funnel_half_w
        if i < n - 1:
            next_val = values[i + 1]
            half_w_bot = (next_val / top_value) * funnel_half_w if top_value > 0 else funnel_half_w
        else:
            half_w_bot = half_w_top * 0.82

        verts = [
            (x_c - half_w_top, y_top),
            (x_c + half_w_top, y_top),
            (x_c + half_w_bot, y_bot),
            (x_c - half_w_bot, y_bot),
        ]
        poly = Polygon(verts, closed=True, facecolor=colour, edgecolor='white',
                        linewidth=2.5, alpha=0.92)
        ax.add_patch(poly)

        mid_y = (y_top + y_bot) / 2
        pct = round(val / top_value * 100, 1) if top_value > 0 else 0

        # Stage label centred inside the trapezoid, clipped to available width
        avg_half_w = (half_w_top + half_w_bot) / 2
        label_fs = max(9, min(11, avg_half_w / funnel_half_w * 13))
        ax.text(x_c, mid_y, stage, ha='center', va='center',
                fontsize=label_fs, fontweight='bold', color='white')
        # Count and percentage to the right of the trapezoid
        ax.text(avg_half_w + 0.35, mid_y,
                f'{int(val):,}  ({pct}%)',
                ha='left', va='center', fontsize=10, color=BRAND_DARK,
                fontweight='bold')

    ax.set_xlim(-funnel_half_w - 0.5, funnel_half_w + 3.0)
    ax.set_ylim(total_h + 0.3, -0.5)

    _save(fig, output_path)


def _slide15_funnel(view_dir, charts_dir):
    """Slide 15 — One trapezoid funnel chart per year."""
    outputs = []

    csv_path = _find_csv(view_dir, '_new_account_conversion_funnel.csv')
    if csv_path is None:
        return outputs

    _load_poppins()
    df = pd.read_csv(csv_path)

    # --- Detect MBR mode (single review month across multiple years) ---
    data_rows = df[
        ~df['Year'].astype(str).isin(['CHANGE']) &
        ~df['Month'].astype(str).str.startswith('TOTAL')
    ]
    numeric_years = pd.to_numeric(df['Year'], errors='coerce').dropna()
    unique_years = sorted(int(y) for y in numeric_years.unique())

    month_name = None
    non_numeric_months = [
        m for m in data_rows['Month'].unique()
        if not str(m).replace('.', '').isdigit()
    ]
    if len(non_numeric_months) == 1:
        month_name = non_numeric_months[0]

    if month_name and len(unique_years) >= 2:
        # --- MBR mode: one funnel image per year ---
        stages = ['Accounts Created', 'Created Any Event', 'Sold Paid Tickets']
        palette = [
            [BRAND_DARK, '#2a5580', '#3d72a8'],
            [BRAND_ACCENT, '#3a99bf', '#52b0d4'],
            ['#F18F01', '#f5a733', '#f7bb5c'],
        ]

        for idx, yr in enumerate(unique_years):
            row = data_rows[data_rows['Year'].astype(str) == str(yr)]
            if row.empty:
                continue
            row = row.iloc[0]
            vals = [float(row.get(s, 0)) if not pd.isna(row.get(s, 0)) else 0
                    for s in stages]

            colours = palette[idx % len(palette)]
            title = f'{month_name} {yr} \u2014 New Account Funnel'
            out = os.path.join(charts_dir, f'slide15_funnel_{yr}.png')
            _draw_funnel_chart(stages, vals, colours, title, out)
            outputs.append(out)

    else:
        # --- EOY fallback: table ---
        key_cols = [c for c in [
            'Month', 'Year', 'Accounts Created', 'Created Any Event',
            'Sold Paid Tickets', 'Conversion Rate (Paid) %',
            'Avg Days to First Event', 'Avg Days to First Sale',
        ] if c in df.columns]

        display_df = df[key_cols].copy() if key_cols else df.head(20)
        title = 'New Account Conversion Funnel'

        out_path = os.path.join(charts_dir, 'slide15_funnel_table.png')
        _brand_table(display_df, title, out_path)
        outputs.append(out_path)

    return outputs


def _slide16_activation(view_dir, charts_dir):
    """Slide 16 — Activation rate: YoY grouped bars (MBR) or by-month (EOY)."""
    outputs = []

    _load_poppins()

    # --- MBR mode: look for the review-month CSV first ---
    review_csv = _find_csv(view_dir, '_activation_review_month.csv', folder='cohorts')
    if review_csv is not None:
        rdf = pd.read_csv(review_csv)

        if 'Year' in rdf.columns and 'Activation Rate %' in rdf.columns:
            month_name = rdf['Month Name'].iloc[0] if 'Month Name' in rdf.columns else ''
            year_colours = [BRAND_DARK, BRAND_ACCENT, '#F18F01']

            # Chart 1: Activation Rate % by year (grouped bars)
            fig, ax = plt.subplots(figsize=(8, 5))
            years = rdf['Year'].astype(str).tolist()
            rates = rdf['Activation Rate %'].tolist()
            colours = year_colours[:len(years)]

            bars = ax.bar(range(len(years)), rates, color=colours, edgecolor='white', width=0.5)
            ax.set_xticks(range(len(years)))
            ax.set_xticklabels(years, fontsize=11)
            for bar, val in zip(bars, rates):
                ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(),
                        f'{val:.1f}%', ha='center', va='bottom', fontsize=9,
                        color=BRAND_DARK, fontweight='bold')
            ax.set_title(f'Activation Rate \u2014 {month_name} YoY', fontsize=13,
                         fontweight='bold', color=BRAND_DARK, pad=10)
            ax.set_ylabel('Activation Rate %', fontsize=10)
            ax.spines['top'].set_visible(False)
            ax.spines['right'].set_visible(False)

            out1 = os.path.join(charts_dir, 'slide16_activation_window.png')
            _save(fig, out1)
            outputs.append(out1)

            # Chart 2: Activation windows (7d/30d/90d) grouped by year
            window_cols = ['Within 7 Days %', 'Within 30 Days %', 'Within 90 Days %']
            if all(c in rdf.columns for c in window_cols):
                fig2, ax2 = plt.subplots(figsize=(8, 5))
                window_labels = ['Within 7 Days', 'Within 30 Days', 'Within 90 Days']
                n_years = len(years)
                n_windows = len(window_labels)
                x = np.arange(n_windows)
                total_width = 0.7
                bar_width = total_width / n_years

                for i, (yr, colour) in enumerate(zip(years, colours)):
                    yr_data = rdf[rdf['Year'].astype(str) == yr]
                    if yr_data.empty:
                        continue
                    vals = [yr_data[col].iloc[0] for col in window_cols]
                    offset = (i - (n_years - 1) / 2) * bar_width
                    bar_objs = ax2.bar(x + offset, vals, bar_width, label=yr,
                                       color=colour, edgecolor='white')
                    for bar, val in zip(bar_objs, vals):
                        ax2.text(bar.get_x() + bar.get_width() / 2, bar.get_height(),
                                 f'{val:.1f}%', ha='center', va='bottom', fontsize=7,
                                 color=BRAND_DARK)

                ax2.set_xticks(x)
                ax2.set_xticklabels(window_labels, fontsize=10)
                ax2.set_title(f'Activation Windows \u2014 {month_name} YoY', fontsize=13,
                              fontweight='bold', color=BRAND_DARK, pad=10)
                ax2.set_ylabel('% of Accounts', fontsize=10)
                ax2.legend(fontsize=9)
                ax2.spines['top'].set_visible(False)
                ax2.spines['right'].set_visible(False)

                out2 = os.path.join(charts_dir, 'slide16_daily_activations.png')
                _save(fig2, out2)
                outputs.append(out2)

        return outputs

    # --- EOY fallback: activation by signup month (all months, aggregated) ---
    csv_path = _find_csv(view_dir, '_activation_by_signup_month.csv', folder='cohorts')
    if csv_path is None:
        return outputs

    df = pd.read_csv(csv_path)

    if 'Signup Month' not in df.columns:
        return outputs

    # Chart 1: Activation rate by signup month
    fig, ax = plt.subplots(figsize=(10, 5))
    months = df['Signup Month'].tolist()
    rates = df['Activation Rate %'].tolist() if 'Activation Rate %' in df.columns else []

    if rates:
        _brand_bar(ax, months, rates, 'Activation Rate by Signup Month',
                   colour=BRAND_ACCENT, fmt_func=_fmt_pct, ylabel='Activation Rate %')
        out1 = os.path.join(charts_dir, 'slide16_activation_window.png')
        _save(fig, out1)
        outputs.append(out1)

    # Chart 2: Activation timing breakdown (within 7/30/90 days)
    if all(c in df.columns for c in ['Within 7 Days %', 'Within 30 Days %', 'Within 90 Days %']):
        fig2, ax2 = plt.subplots(figsize=(10, 5))
        x = np.arange(len(months))
        width = 0.25

        ax2.bar(x - width, df['Within 7 Days %'], width, label='Within 7 Days',
                color=BRAND_DARK, edgecolor='white')
        ax2.bar(x, df['Within 30 Days %'], width, label='Within 30 Days',
                color=BRAND_ACCENT, edgecolor='white')
        ax2.bar(x + width, df['Within 90 Days %'], width, label='Within 90 Days',
                color='#90CAF9', edgecolor='white')

        ax2.set_xticks(x)
        ax2.set_xticklabels(months, fontsize=8, rotation=45, ha='right')
        ax2.set_title('Activation Windows by Signup Month', fontsize=13,
                      fontweight='bold', color=BRAND_DARK)
        ax2.set_ylabel('% of Accounts', fontsize=10)
        ax2.legend(fontsize=9)
        ax2.spines['top'].set_visible(False)
        ax2.spines['right'].set_visible(False)

        out2 = os.path.join(charts_dir, 'slide16_daily_activations.png')
        _save(fig2, out2)
        outputs.append(out2)

    return outputs


def _slides_17_20_keywords(view_dir, charts_dir):
    """Slides 17-20 — Keyword word clouds (volume + value, CY vs PY month)."""
    outputs = []

    _load_poppins()

    # Try wordcloud, fall back to bar chart
    try:
        from wordcloud import WordCloud
        has_wordcloud = True
    except ImportError:
        has_wordcloud = False

    # Read period labels
    cy_label, py_label = _read_period_labels(view_dir)

    # Load CY and PY keyword frequency data
    cy_csv = _find_csv(view_dir, '_keywords_frequency_cy.csv', folder='keywords')
    py_csv = _find_csv(view_dir, '_keywords_frequency_py.csv', folder='keywords')

    # Fall back to combined file if CY/PY not available
    if cy_csv is None:
        cy_csv = _find_csv(view_dir, '_keywords_frequency.csv', folder='keywords')
        cy_label = cy_label if cy_label != 'Current Year' else 'Month'
    if py_csv is None:
        py_csv = cy_csv  # Graceful fallback
        py_label = py_label if py_label != 'Previous Year' else 'Previous Year'

    if cy_csv is None:
        return outputs

    cy_df = pd.read_csv(cy_csv)
    if 'Keyword' not in cy_df.columns:
        return outputs

    cy_top = cy_df.nlargest(100, 'Event Count') if 'Event Count' in cy_df.columns else cy_df.head(100)

    py_top = pd.DataFrame()
    if py_csv and os.path.exists(py_csv):
        py_df = pd.read_csv(py_csv)
        if 'Keyword' in py_df.columns:
            py_top = py_df.nlargest(100, 'Event Count') if 'Event Count' in py_df.columns else py_df.head(100)

    # Define the four keyword charts: CY volume, PY volume, CY value, PY value
    charts = [
        ('slide17_keywords_volume_cy.png', 'Event Count', f'Keywords by Volume ({cy_label})', cy_top),
        ('slide18_keywords_volume_py.png', 'Event Count', f'Keywords by Volume ({py_label})', py_top),
        ('slide19_keywords_value_cy.png', 'Total Fees', f'Keywords by Value ({cy_label})', cy_top),
        ('slide20_keywords_value_py.png', 'Total Fees', f'Keywords by Value ({py_label})', py_top),
    ]

    for filename, weight_col, title, data in charts:
        if data.empty or weight_col not in data.columns:
            continue

        # Filter to positive values only
        chart_data = data[data[weight_col] > 0].copy()
        if chart_data.empty:
            continue

        out_path = os.path.join(charts_dir, filename)

        if has_wordcloud:
            _generate_wordcloud(chart_data, weight_col, title, out_path)
        else:
            _generate_keyword_bars(chart_data, weight_col, title, out_path)

        outputs.append(out_path)

    return outputs


def _generate_wordcloud(df, weight_col, title, output_path, max_words=100):
    """Generate a word cloud image with weight-based colouring and a top-5 side panel."""
    from wordcloud import WordCloud
    from matplotlib.colors import LinearSegmentedColormap

    # Build frequency dict
    freq = dict(zip(df['Keyword'], df[weight_col]))

    # Use Poppins font if available
    font_path = os.path.join(_FONT_DIR, 'Poppins-SemiBold.ttf')
    if not os.path.exists(font_path):
        font_path = None

    # Weight-based colour function: higher-weighted words get darker/warmer colours
    max_val = max(freq.values()) if freq else 1
    # Brand colour gradient: light blue -> brand accent -> dark navy
    cmap = LinearSegmentedColormap.from_list('brand_weight', [
        '#90CAF9',   # Light blue (low weight)
        '#2E86AB',   # Brand accent (mid weight)
        '#F18F01',   # Warm orange (high weight)
        '#1B3A5C',   # Brand dark (top weight)
    ])

    def _weight_colour(word, font_size, position, orientation, random_state=None, **kwargs):
        weight = freq.get(word, 0)
        normalised = weight / max_val if max_val > 0 else 0
        rgba = cmap(normalised)
        return '#{:02x}{:02x}{:02x}'.format(int(rgba[0] * 255), int(rgba[1] * 255), int(rgba[2] * 255))

    wc = WordCloud(
        width=1200, height=700,
        max_words=max_words,
        background_color='white',
        font_path=font_path,
        color_func=_weight_colour,
        prefer_horizontal=0.7,
        min_font_size=10,
        max_font_size=120,
    )
    wc.generate_from_frequencies(freq)

    # Layout: wordcloud on left, top-5 panel on right
    fig = plt.figure(figsize=(16, 7))
    gs = fig.add_gridspec(1, 2, width_ratios=[3.5, 1], wspace=0.05)

    # Wordcloud axes
    ax_cloud = fig.add_subplot(gs[0])
    ax_cloud.imshow(wc, interpolation='bilinear')
    ax_cloud.axis('off')

    # Top 5 side panel
    ax_panel = fig.add_subplot(gs[1])
    ax_panel.axis('off')

    top5 = df.nlargest(5, weight_col)
    fmt_func = _fmt_currency if 'Fees' in weight_col or 'Revenue' in weight_col else _fmt_number

    ax_panel.text(0.05, 0.95, 'Top 5', fontsize=14, fontweight='bold',
                  color=BRAND_DARK, transform=ax_panel.transAxes, va='top')

    # Colour swatches matching the wordcloud gradient
    for i, (_, row) in enumerate(top5.iterrows()):
        y_pos = 0.82 - i * 0.16
        keyword = row['Keyword']
        value = row[weight_col]
        normalised = value / max_val if max_val > 0 else 0
        rgba = cmap(normalised)
        swatch_colour = '#{:02x}{:02x}{:02x}'.format(
            int(rgba[0] * 255), int(rgba[1] * 255), int(rgba[2] * 255))

        # Colour swatch
        ax_panel.add_patch(plt.Rectangle(
            (0.05, y_pos - 0.02), 0.06, 0.08,
            transform=ax_panel.transAxes, facecolor=swatch_colour,
            edgecolor='none', clip_on=False))

        # Keyword name and value
        ax_panel.text(0.15, y_pos + 0.03, keyword.replace('_', ' ').title(),
                      fontsize=11, fontweight='bold', color=BRAND_DARK,
                      transform=ax_panel.transAxes, va='top')
        ax_panel.text(0.15, y_pos - 0.02, fmt_func(value),
                      fontsize=10, color='#666',
                      transform=ax_panel.transAxes, va='top')

    fig.suptitle(title, fontsize=14, fontweight='bold', color=BRAND_DARK, y=0.98)
    _save(fig, output_path)


def _generate_keyword_bars(df, weight_col, title, output_path, top_n=30):
    """Generate a horizontal bar chart as fallback when wordcloud is unavailable."""
    chart_data = df.nlargest(top_n, weight_col)

    fig, ax = plt.subplots(figsize=(10, max(5, top_n * 0.3)))

    keywords = chart_data['Keyword'].tolist()[::-1]  # Reverse for horizontal bar
    values = chart_data[weight_col].tolist()[::-1]

    fmt_func = _fmt_currency if 'Fees' in weight_col or 'Revenue' in weight_col else _fmt_number
    _brand_bar(ax, keywords, values, title, colour=BRAND_ACCENT,
               horizontal=True, fmt_func=fmt_func)

    _save(fig, output_path)


# ---------------------------------------------------------------------------
# CSV discovery helpers
# ---------------------------------------------------------------------------

def _read_period_labels(view_dir):
    """Read CY/PY period labels from the free-paid labels CSV.

    Returns:
        Tuple of (cy_label, py_label) e.g. ('January 2026', 'January 2025').
        Falls back to ('Current Year', 'Previous Year') if unavailable.
    """
    label_csv = _find_csv(view_dir, '_free_paid_labels.csv', folder='industry')
    cy_label = 'Current Year'
    py_label = 'Previous Year'
    if label_csv:
        labels_df = pd.read_csv(label_csv)
        cy_row = labels_df[labels_df['Period'] == 'CY']
        py_row = labels_df[labels_df['Period'] == 'PY']
        if not cy_row.empty:
            cy_label = cy_row.iloc[0]['Label']
        if not py_row.empty:
            py_label = py_row.iloc[0]['Label']
    return cy_label, py_label


def _find_csv(view_dir, suffix, folder=None):
    """
    Find a CSV file in the view directory matching the given suffix.

    Args:
        view_dir: Path to the view directory (e.g., mbr_2026-01/month)
        suffix: Filename suffix to match (e.g., '_industry_by_tier.csv')
        folder: Optional subfolder to search in (e.g., 'industry')

    Returns:
        Path to the matching CSV file, or None if not found
    """
    if folder:
        search_dir = os.path.join(view_dir, folder)
    else:
        search_dir = view_dir

    if not os.path.isdir(search_dir):
        return None

    pattern = os.path.join(search_dir, f'*{suffix}')
    matches = sorted(glob_module.glob(pattern))
    return matches[0] if matches else None


def _month_abbr(month_num):
    """Get month abbreviation from number."""
    import calendar
    return calendar.month_abbr[month_num]


# ---------------------------------------------------------------------------
# Master orchestrator
# ---------------------------------------------------------------------------

def generate_all_charts(view_dir):
    """
    Generate all MBR charts for a single view directory.

    Scans for known CSV files and generates corresponding PNG charts.

    Args:
        view_dir: Path to the view directory (e.g., mbr_2026-01/month)

    Returns:
        Dictionary mapping slide names to lists of generated PNG paths
    """
    _load_poppins()

    charts_dir = os.path.join(view_dir, 'charts')
    os.makedirs(charts_dir, exist_ok=True)

    chart_files = {}

    # Run each slide generator
    generators = [
        ('slide03_targets', _slide03_targets),
        ('slide04_new_accounts', _slide04_new_accounts),
        ('slide05_performance', _slide05_performance),
        ('slide06_revenue', _slide06_revenue),
        ('slide08_all_industry', _slide08_all_industry),
        ('slide09_hv_industry', _slide09_hv_industry),
        ('slide10_new_industry', _slide10_new_industry),
        ('slide11_free_paid', _slide11_free_paid),
        ('slide12_avg_metrics', _slide12_avg_metrics),
        ('slide14_retention', _slide14_retention),
        ('slide15_funnel', _slide15_funnel),
        ('slide16_activation', _slide16_activation),
        ('slides_17_20_keywords', _slides_17_20_keywords),
    ]

    total_images = 0
    for name, gen_func in generators:
        try:
            paths = gen_func(view_dir, charts_dir)
            if paths:
                chart_files[name] = paths
                total_images += len(paths)
        except Exception as e:
            print(f"    Warning: Chart generation failed for {name}: {e}")

    # Write README mapping each PNG to its slide
    readme_path = os.path.join(charts_dir, 'README.txt')
    _write_chart_readme(readme_path, chart_files)

    print(f"  \u2713 Charts: {total_images} images in {charts_dir}/")
    return chart_files


def _write_chart_readme(readme_path, chart_files):
    """Write a README.txt mapping each generated PNG to its target slide."""
    slide_descriptions = {
        'slide03_targets': 'Slide 3 — New Account Targets',
        'slide04_new_accounts': 'Slide 4 — New Account Dynamics',
        'slide05_performance': 'Slide 5 — Monthly Performance YoY',
        'slide06_revenue': 'Slide 6 — Revenue',
        'slide08_all_industry': 'Slide 8 — All Clients Industry (CY vs PY Fees)',
        'slide09_hv_industry': 'Slide 9 — Key + High Value Industry (CY vs PY Fees)',
        'slide10_new_industry': 'Slide 10 — New Accounts (Last 3 Months) Industry (CY vs PY Fees)',
        'slide11_free_paid': 'Slide 11 — Free/Paid Split',
        'slide12_avg_metrics': 'Slide 12 — Average Ticket Value',
        'slide14_retention': 'Slide 14 — Retention',
        'slide15_funnel': 'Slide 15 — Activation Funnel',
        'slide16_activation': 'Slide 16 — First 7 Days Activation',
        'slides_17_20_keywords': 'Slides 17-20 — Keyword Analysis (CY vs PY Volume & Value)',
    }

    with open(readme_path, 'w') as f:
        f.write('MBR Chart Image Guide\n')
        f.write('=' * 50 + '\n\n')
        f.write('Each PNG below corresponds to a slide in the Monthly Business Review PPTX.\n')
        f.write('Drag and drop into the relevant slide.\n\n')

        for name, paths in chart_files.items():
            desc = slide_descriptions.get(name, name)
            f.write(f'{desc}\n')
            f.write('-' * len(desc) + '\n')
            for p in paths:
                f.write(f'  {os.path.basename(p)}\n')
            f.write('\n')
