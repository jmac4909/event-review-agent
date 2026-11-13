"""Demo identities are deliberately fixed. Region authority lives on the server."""

from fastapi import HTTPException, Request

from .models import Supervisor


SUPERVISORS = {
    account.id: account
    for account in (
        Supervisor(id="supervisor-europe", name="Europe supervisor", region="europe", region_label="Europe"),
        Supervisor(id="supervisor-us", name="US supervisor", region="us", region_label="United States"),
        Supervisor(id="supervisor-asia", name="Asia supervisor", region="asia", region_label="Asia"),
    )
}


def current_supervisor(request: Request) -> Supervisor:
    account = SUPERVISORS.get(request.session.get("account_id"))
    access_version = request.app.state.preview_access_version
    if access_version and request.session.get("access_version") != access_version:
        raise HTTPException(status_code=401, detail="Enter the preview access code and choose a region.")
    if account is None:
        raise HTTPException(status_code=401, detail="Choose a demo supervisor to sign in.")
    expected = request.headers.get("X-Expected-Supervisor")
    if expected is not None and expected != account.id:
        # A precondition, never authority: the signed session still owns region.
        raise HTTPException(status_code=412, detail="Your session changed in another tab. Choose a region again.")
    return account

