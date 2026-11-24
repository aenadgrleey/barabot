import logging

from barabot.core.logs import NOTICE, SyslogPrefixFormatter


def record(level, msg):
    return logging.LogRecord("barabot", level, __file__, 1, msg, None, None)


def test_every_line_gets_its_journald_priority():
    fmt = SyslogPrefixFormatter("%(message)s")
    assert fmt.format(record(NOTICE, "Match: a\nsecond line")) == "<5>Match: a\n<5>second line"
    assert fmt.format(record(logging.ERROR, "boom")) == "<3>boom"
    assert fmt.format(record(logging.INFO, "decided")) == "<6>decided"
    assert fmt.format(record(logging.WARNING, "slow")) == "<4>slow"
