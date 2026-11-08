from app.main import app


def test_tenant_botfather_provisioning_routes_are_not_exposed():
    registered = {
        (route.path, method)
        for route in app.routes
        for method in (getattr(route, "methods", None) or set())
    }

    assert ("/telegram/connection", "GET") in registered
    assert ("/telegram/connection", "POST") not in registered
    assert ("/telegram/connection/complete", "POST") not in registered
