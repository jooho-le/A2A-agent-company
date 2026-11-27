"""Cleanup callbacks for borrowed unittest fixtures used by the offline pipeline tests."""


def adopt_cleanups(owner, fixture):
    """Run borrowed fixture cleanup on the active TestCase's asyncio runner.

    Calling doCleanups on an IsolatedAsyncioTestCase never run by unittest
    cannot call its cleanup functions: it has no asyncio runner. Transfer
    callbacks instead, preserving their LIFO order through nested fixtures.
    """
    for function, arguments, keywords in fixture._cleanups:
        owner.addCleanup(function, *arguments, **keywords)
    fixture._cleanups.clear()


def set_up_borrowed(owner, fixture):
    try:
        fixture.setUp()
    finally:
        adopt_cleanups(owner, fixture)
