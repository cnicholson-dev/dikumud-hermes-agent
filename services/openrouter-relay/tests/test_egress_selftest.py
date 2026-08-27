"""SEC-07: the relay refuses to start when its egress is not restricted.

The firewall rules are host state. They do not survive a reboot on their own,
`docker compose up` does not apply them, and their absence has no symptom,
because the relay's pinned upstream keeps the deployment working either way.
These tests cover the check that turns that silent gap into a startup failure.
"""
import pytest

from openrouter_relay import egress


@pytest.fixture
def reachable(monkeypatch):
    """The canary answers: this host has general egress."""
    monkeypatch.setattr(egress, "canary_reachable", lambda *a, **k: True)


@pytest.fixture
def unreachable(monkeypatch):
    """The canary is blocked: the rules are in place."""
    monkeypatch.setattr(egress, "canary_reachable", lambda *a, **k: False)


def test_it_refuses_to_start_when_the_canary_is_reachable(reachable):
    with pytest.raises(egress.EgressNotRestricted) as err:
        egress.enforce({})
    message = str(err.value)
    assert "SEC-07" in message
    # The message has to be actionable at 3am, so it names the fix.
    assert "restrict-relay-egress apply" in message


def test_it_starts_when_the_canary_is_blocked(unreachable):
    assert "egress restricted" in egress.enforce({})


def test_the_self_test_is_opt_out_not_opt_in(reachable):
    # Forgetting to configure anything must fail closed, so an absent variable
    # means the check runs.
    with pytest.raises(egress.EgressNotRestricted):
        egress.enforce({})
    with pytest.raises(egress.EgressNotRestricted):
        egress.enforce({"RELAY_EGRESS_SELFTEST": "1"})

    # And an explicit opt-out is honoured, for a deployment that restricts
    # egress by another mechanism.
    assert "disabled" in egress.enforce({"RELAY_EGRESS_SELFTEST": "0"})


@pytest.mark.parametrize("value", ["0", "no", "false", "off", ""])
def test_only_affirmative_values_keep_the_check_on(reachable, value):
    result = egress.enforce({"RELAY_EGRESS_SELFTEST": value})
    assert "disabled" in result


def test_the_canary_is_configurable(monkeypatch):
    seen = {}

    def fake(host, port, timeout):
        seen.update(host=host, port=port, timeout=timeout)
        return False

    monkeypatch.setattr(egress, "canary_reachable", fake)
    egress.enforce({"RELAY_EGRESS_CANARY_HOST": "192.0.2.1",
                    "RELAY_EGRESS_CANARY_PORT": "8443",
                    "RELAY_EGRESS_CANARY_TIMEOUT": "1.5"})
    assert seen == {"host": "192.0.2.1", "port": 8443, "timeout": 1.5}


def test_the_default_canary_is_an_ip_literal_not_a_hostname():
    # DNS is denied by the same rules, so a hostname would fail to resolve and
    # look exactly like a blocked connection: the check would pass for the
    # wrong reason.
    host = egress.DEFAULT_CANARY_HOST
    assert all(part.isdigit() for part in host.split("."))


def test_a_connection_error_reads_as_blocked(monkeypatch):
    def refuse(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(egress.socket, "create_connection", refuse)
    assert egress.canary_reachable("1.1.1.1", 443, 0.1) is False
