# itambox/core/constants.py

# Define choices for the number of objects to display per page
PAGINATE_COUNT_CHOICES = (
    (25, "25"),
    (50, "50"),
    (100, "100"),
    (250, "250"),
    (500, "500"),
    # Add a large number for "All" - use caution with performance
    # (1000000, 'All'),
)

# Default per page count
DEFAULT_PAGINATE_COUNT = 50

# --- Tabbed List View HTMX IDs ---
RESULTS_PANE_CONTENT_ID = "object-list-dynamic-content"
RESULTS_TAB_COUNT_ID = "results-tab-count"
FILTERS_APPLIED_COUNT_ID = "filters-applied-count"
