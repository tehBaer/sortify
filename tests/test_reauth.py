"""Re-authorising while already logged in.

Scopes change (playback control was added), and a token only carries the
scopes it was issued with. The setup view was reachable only when authed was
false, so the one user who needs to re-auth — someone already logged in with
an older token — was the one user who could not get to it.
"""

import pytest

from sortify import app as appmod


def test_reconnecting_reuses_the_stored_client_id():
    """Re-auth is not first-time setup: the client ID is already known, and
    making the user fetch it from the Spotify dashboard again to add a scope
    is a papercut on the one flow that is already fiddly."""
    appmod.store.update_config(client_id="stored-id")

    url = appmod.auth_start(appmod.AuthStart(client_id=""))["auth_url"]

    assert "client_id=stored-id" in url


def test_a_first_time_setup_still_demands_a_client_id():
    appmod.store.update_config(client_id=None)

    with pytest.raises(appmod.HTTPException) as e:
        appmod.auth_start(appmod.AuthStart(client_id=""))
    assert e.value.status_code == 400


def test_the_new_scope_is_in_the_url_the_user_is_sent_to():
    """The whole point of re-authing: without this in the consent screen the
    new token comes back just as unable to control playback as the old one."""
    appmod.store.update_config(client_id="stored-id")

    url = appmod.auth_start(appmod.AuthStart(client_id=""))["auth_url"]

    assert "user-modify-playback-state" in url
