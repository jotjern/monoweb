#!/usr/bin/env python3
"""Live smoke test: TLS, synthetic content, authorization, writes, and branch isolation."""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from previewctl import Controller, http

def call(state, procedure, value=None, token=None, mutation=False):
    url = state["rpc_origin"] + "/api/trpc/" + procedure
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    body = None
    if mutation:
        body = json.dumps({"json": value}).encode()
    elif value is not None:
        url += "?" + urllib.parse.urlencode({"input": json.dumps({"json": value})})
    request = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(request, timeout=45) as response:
        result = json.load(response)
    return result["result"]["data"]["json"]

def login(controller, state):
    # Temporarily enable ROPG for the synthetic account test, then restore the
    # application's original grants even if the token request fails.
    client_id = state["web_client"]["client_id"]
    original = controller.auth("GET", "/clients/" + client_id)["grant_types"]
    try:
        controller.auth("PATCH", "/clients/" + client_id, {"grant_types": list(set(original + ["password", "http://auth0.com/oauth/grant-type/password-realm"]))})
        result = http("POST", "https://" + controller.config["auth0_domain"] + "/oauth/token", {
            "grant_type": "http://auth0.com/oauth/grant-type/password-realm",
            "client_id": client_id, "client_secret": state["web_client"]["client_secret"],
            "username": state["admin"]["email"], "password": state["admin_password"],
            "realm": state["connection"]["name"], "audience": state["rpc_origin"],
            "scope": "openid profile email",
        })
        return result["access_token"]
    finally:
        controller.auth("PATCH", "/clients/" + client_id, {"grant_types": original})

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


controller = Controller()
states = [json.loads(controller.state_path(preview).read_text()) for preview in sys.argv[1:]]
assert states, "Provide at least one preview ID"
for state in states:
    with urllib.request.urlopen(state["origin"], timeout=45) as response:
        html = response.read().decode()
        assert response.status == 200 and "Online" in html
    opener = urllib.request.build_opener(NoRedirect)
    for path in ("/admin", "/api/auth/authorize"):
        try:
            opener.open(state["origin"] + path, timeout=45)
        except urllib.error.HTTPError as response:
            assert response.code in (302, 303, 307, 308)
            target = urllib.parse.urlparse(urllib.parse.urljoin(state["origin"], response.headers["Location"]))
            if path == "/admin":
                assert target.hostname == urllib.parse.urlparse(state["origin"]).hostname
            else:
                assert target.hostname == controller.config["auth0_domain"]
                parameters = urllib.parse.parse_qs(target.query)
                assert parameters["redirect_uri"] == [state["origin"] + "/api/auth/callback/auth0"]
                assert parameters["client_id"] == [state["web_client"]["client_id"]]
                assert parameters["audience"] == [state["rpc_origin"]]
        else:
            raise AssertionError("Login/admin route did not redirect")
    assert http("GET", state["rpc_origin"] + "/health") == {"status": "ok"}
    article = call(state, "article.featured")[0]
    token = login(controller, state)
    assert call(state, "user.isAdmin", token=token) is True
    assert call(state, "user.isAdmin") is False
    edit = {"id": article["id"], "input": {"excerpt": "Preview isolation smoke test"}, "tags": []}
    try:
        call(state, "article.edit", edit, mutation=True)
    except urllib.error.HTTPError as error:
        assert error.code in (401, 403), error.code
    else:
        raise AssertionError("Anonymous mutation succeeded")
    try:
        call(state, "article.edit", edit, token=token, mutation=True)
        assert call(state, "article.getBySlug", article["slug"])["excerpt"] == "Preview isolation smoke test"
        for peer in states:
            if peer["id"] != state["id"]:
                assert call(peer, "article.getBySlug", article["slug"])["excerpt"] != "Preview isolation smoke test"
                try:
                    call(peer, "user.isAdmin", token=token)
                except urllib.error.HTTPError:
                    pass
                else:
                    raise AssertionError("Another preview accepted this preview's token")
    finally:
        edit["input"]["excerpt"] = article["excerpt"]
        call(state, "article.edit", edit, token=token, mutation=True)
    print(f"PASS {state['id']}: TLS, synthetic data, test login, admin authorization, anonymous rejection, and write/restore")
if len(states) > 1:
    assert len({s["branch_id"] for s in states}) == len(states)
    assert len({s["db_runtime"]["username"] for s in states}) == len(states)
    print("PASS independent PlanetScale branches, credentials, data writes, and API audiences")
