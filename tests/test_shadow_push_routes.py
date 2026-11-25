"""Discord routing fix (2026-11-25, Josh): the four display-only
shadow books — Stock Sweeps, SH-SPY, Connors SSC, C2 — drain their
own outboxes to the research route (#compass), not the options_0dte
route (#0dte), which belongs to the 0DTE scanner funnel alone.

Each module resolves its destination at drain time through
alert_routes.destination and outbox rows persist no route, so a
route change redirects still-queued rows automatically; these tests
pin the resolved route for each module."""
from types import SimpleNamespace

import pytest

from compass import connors_ssc, sh_spy_options, stock_sweeps, swings_c2

RESEARCH_URL = 'https://discord.com/api/webhooks/111111111111111111/research'
ODTE_URL = 'https://discord.com/api/webhooks/222222222222222222/odte'


def _cfg():
    return SimpleNamespace(
        discord_routes={'research': RESEARCH_URL, 'options_0dte': ODTE_URL},
        discord_fallback=False, discord='')


@pytest.mark.parametrize('module', [stock_sweeps, sh_spy_options,
                                    connors_ssc, swings_c2],
                         ids=['stock_sweeps', 'sh_spy_options',
                              'connors_ssc', 'swings_c2'])
def test_shadow_push_destination_is_research_route(module):
    url, mode = module.push_destination(_cfg())
    assert url == RESEARCH_URL and mode == 'dedicated'
