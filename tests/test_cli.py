from sortify.cli import DEV_CAP, dev_call_allowed


def test_dev_ceiling_blocks_dev_traffic():
    assert dev_call_allowed(0)
    assert dev_call_allowed(DEV_CAP - 1)
    assert not dev_call_allowed(DEV_CAP)
    assert not dev_call_allowed(DEV_CAP + 500)
