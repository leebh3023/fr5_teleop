from teleop.controller_lease import ControllerLease


def test_only_one_controller_owns_the_lease() -> None:
    lease = ControllerLease()
    assert lease.claim("one")
    assert not lease.claim("two")
    assert lease.accept_sequence("one", 1)
    assert not lease.accept_sequence("one", 1)
    assert lease.release("one")
    assert lease.claim("two")
