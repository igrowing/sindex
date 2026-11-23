"""Installing the service from the Python environment twice: the second `sindex install` must update it."""

from pathlib import Path

import pytest
import pywintypes

from sindex import cli, service

EXE = Path(r"C:\venv\sindex-service\pythonservice.exe")
ALREADY_THERE = pywintypes.error(1073, "CreateService", "The specified service already exists.")


class FakeServiceUtil:
    """win32serviceutil as far as the install uses it; `installing` is what InstallService does."""

    def __init__(self, installing=None) -> None:
        self.installing = installing
        self.calls: list[tuple[str, tuple, dict]] = []

    def InstallService(self, *args, **kwargs) -> None:  # noqa: N802  (pywin32's name)
        self.calls.append(("install", args, kwargs))
        if self.installing is not None:
            raise self.installing

    def ChangeServiceConfig(self, *args, **kwargs) -> None:  # noqa: N802  (pywin32's name)
        self.calls.append(("change", args, kwargs))


@pytest.fixture
def fake(monkeypatch):
    def use(installing=None):
        util = FakeServiceUtil(installing)
        monkeypatch.setattr(service, "win32serviceutil", util)
        return util
    return use


def test_a_first_install_registers_the_service(fake):
    util = fake()

    assert service.install_python_service(EXE) is False

    assert [call[0] for call in util.calls] == ["install"]


def test_installing_again_updates_the_registration_instead_of_failing(fake):
    util = fake(ALREADY_THERE)

    assert service.install_python_service(EXE) is True

    kinds = [call[0] for call in util.calls]
    assert kinds == ["install", "change"]
    _, args, kwargs = util.calls[1]
    assert args[:2] == ("sindex.service.SindexService", service.SERVICE_NAME)
    assert kwargs["exeName"] == str(EXE) and kwargs["delayedstart"] is True


def test_any_other_failure_of_the_install_is_not_hidden(fake):
    util = fake(pywintypes.error(5, "CreateService", "Access is denied."))

    with pytest.raises(pywintypes.error, match="Access is denied"):
        service.install_python_service(EXE)

    assert [call[0] for call in util.calls] == ["install"]


def test_a_reinstall_restarts_a_service_that_was_running():
    assert cli._restart_after_install(requested=False, was_running=True)


def test_an_install_leaves_a_stopped_or_new_service_stopped_unless_asked():
    assert not cli._restart_after_install(requested=False, was_running=False)
    assert cli._restart_after_install(requested=True, was_running=False)
