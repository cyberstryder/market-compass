"""Stable alert families; destinations never change strategy or sender ownership."""
from urllib.parse import urlparse
from .alert_format import alert_identity

# Independent delivery queues. Source mirrors and experiments have their own home.
ROUTES = {
    'spy_morning': ('spy-0dte-plan', 'SPY morning brief + TradingView drawing prompt'),
    'futures': ('futures', 'Futures signals, observations and simulated management'),
    'options_0dte': ('options-0dte', 'Compass same-day option simulations'),
    'options_ideas': ('options-ideas', 'All-day options ideas; existing 1–21 DTE policy'),
    'swing': ('swing-ideas', 'Multi-session stock and option ideas'),
    'unusual_options': ('unusual-options', 'Unusual flow confirmed by a price setup'),
    'exposure': ('exposure-levels', 'Exposure-level price setups'),
    'intraday': ('intraday-stocks', 'Intraday stock / ETF setups and simulations'),
    'research': ('compass-research', 'Secondary reviews, setup results and original-app mirrors'),
    'system': ('compass-system', 'Service notices and explicit delivery tests'),
}


def route_for(row):
    p = row['payload']
    if p.get('status') == 'notification_test':
        return p.get('delivery_route') if p.get('delivery_route') in ROUTES else 'system'
    category = alert_identity(row)['category']
    if (p.get('status') in ('secondary_review', 'setup_result') or
            category in ('morning', 'smoothers')):
        return 'research'
    if category in ROUTES:
        return category
    return {'swing_ideas': 'swing', 'end_of_day_algo': 'research'}.get(category, 'system')


def variable(route):
    return 'DISCORD_' + route.upper() + '_WEBHOOK_URL'


def valid_webhook(value):
    try:
        url = urlparse(value)
        parts = url.path.strip('/').split('/')
        return (url.scheme == 'https' and url.hostname in ('discord.com', 'discordapp.com')
                and url.port in (None, 443) and not url.username and not url.password
                and len(parts) == 4 and parts[:2] == ['api', 'webhooks'] and all(parts[2:]))
    except ValueError:
        return False


def destination(cfg, route):
    dedicated = cfg.discord_routes.get(route, '')
    if dedicated:
        return dedicated, 'dedicated' if valid_webhook(dedicated) else 'invalid'
    if cfg.discord_fallback and cfg.discord:
        return cfg.discord, 'shared_fallback' if valid_webhook(cfg.discord) else 'invalid'
    return '', 'awaiting_channel'


def manifest(cfg):
    # No URLs, tokens or credentials belong in dashboard/state/log output.
    return [dict(route=key, channel=channel, description=description, variable=variable(key),
                 destination_mode=destination(cfg, key)[1], sender='Compass')
            for key, (channel, description) in ROUTES.items()]


ORIGINAL_SENDERS = [
    {'route': 'morning', 'channel': 'morning-algo', 'description': 'Original Morning Algo official alerts',
     'sender': 'Morning Algo Tracker', 'destination_mode': 'original_sender',
     'variable': 'DISCORD_WEBHOOK_URL on Morning Algo Tracker'},
    {'route': 'smoothers', 'channel': 'smoothers', 'description': 'Original Smoothers weekly entries and management',
     'sender': 'smoothers-new', 'destination_mode': 'original_sender',
     'variable': 'DISCORD_WEBHOOK_URL on smoothers-new'},
]
