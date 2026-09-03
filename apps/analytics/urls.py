from django.urls import path

from . import agents, dashboard, meta, widgets

urlpatterns = [
    path("analytics/dashboard", dashboard.dashboard_stats),
    path("analytics/messages", dashboard.message_analytics),
    path("analytics/chatbot", dashboard.chatbot_analytics),
    path("analytics/agents/comparison", agents.agent_comparison),
    path("analytics/agents/<uuid:agent_id>", agents.agent_details),
    path("analytics/agents", agents.agent_analytics),
    path("analytics/meta/accounts", meta.meta_accounts),
    path("analytics/meta/refresh", meta.refresh_meta_cache),
    path("analytics/meta", meta.meta_analytics),
    path("widgets/data-sources", widgets.data_sources),
    path("widgets/data", widgets.all_widgets_data),
    path("widgets/layout", widgets.save_layout),
    path("widgets/<uuid:widget_id>/data", widgets.widget_data),
    path("widgets/<uuid:widget_id>", widgets.widget_detail),
    path("widgets", widgets.widgets_collection),
]
