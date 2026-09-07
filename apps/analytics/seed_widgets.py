"""Default dashboard widgets — same set Go SeedDefaultWidgetsForOrg creates."""

from apps.analytics.models import Widget

DEFAULT_WIDGETS = (
    {
        "name": "Total Messages",
        "description": "Total number of messages sent and received",
        "data_source": "messages",
        "display_type": "number",
        "color": "blue",
        "config": {},
        "display_order": 1,
        "grid_x": 0,
        "grid_y": 0,
        "grid_w": 3,
        "grid_h": 3,
    },
    {
        "name": "Active Contacts",
        "description": "Number of contacts with recent activity",
        "data_source": "contacts",
        "display_type": "number",
        "color": "green",
        "config": {},
        "display_order": 2,
        "grid_x": 3,
        "grid_y": 0,
        "grid_w": 3,
        "grid_h": 3,
    },
    {
        "name": "Chatbot Sessions",
        "description": "Active chatbot conversation sessions",
        "data_source": "sessions",
        "display_type": "number",
        "color": "purple",
        "config": {},
        "display_order": 3,
        "grid_x": 6,
        "grid_y": 0,
        "grid_w": 3,
        "grid_h": 3,
    },
    {
        "name": "Total Campaigns",
        "description": "Number of bulk message campaigns",
        "data_source": "campaigns",
        "display_type": "number",
        "color": "orange",
        "config": {},
        "display_order": 4,
        "grid_x": 9,
        "grid_y": 0,
        "grid_w": 3,
        "grid_h": 3,
    },
    {
        "name": "Recent Messages",
        "description": "Latest conversations from your contacts",
        "data_source": "messages",
        "display_type": "table",
        "color": "",
        "config": {},
        "display_order": 5,
        "grid_x": 0,
        "grid_y": 3,
        "grid_w": 6,
        "grid_h": 8,
    },
    {
        "name": "Quick Actions",
        "description": "Common tasks and shortcuts",
        "data_source": "shortcuts",
        "display_type": "shortcuts",
        "color": "",
        "config": {"shortcuts": ["chat", "campaigns", "templates", "chatbot"]},
        "display_order": 6,
        "grid_x": 6,
        "grid_y": 3,
        "grid_w": 6,
        "grid_h": 8,
    },
)


def seed_default_widgets_for_org(organization, user) -> int:
    if Widget.objects.filter(organization=organization).exists():
        return 0
    created = 0
    for item in DEFAULT_WIDGETS:
        Widget.objects.create(
            organization=organization,
            user=user,
            name=item["name"],
            description=item["description"],
            data_source=item["data_source"],
            metric="count",
            display_type=item["display_type"],
            show_change=item["display_type"] == "number",
            color=item["color"],
            size="small",
            config=item["config"],
            display_order=item["display_order"],
            grid_x=item["grid_x"],
            grid_y=item["grid_y"],
            grid_w=item["grid_w"],
            grid_h=item["grid_h"],
            is_shared=True,
            is_default=True,
        )
        created += 1
    return created


def seed_default_widgets() -> int:
    from apps.accounts.models import Organization, User

    admin = User.objects.filter(email="admin@admin.com").first()
    if admin is None:
        return 0
    total = 0
    for org in Organization.objects.all():
        owner = admin if admin.organization_id == org.id else (User.objects.filter(organization=org).first() or admin)
        total += seed_default_widgets_for_org(org, owner)
    return total
