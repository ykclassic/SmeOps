import os

# TestClient and async integration tests use different event loops.
os.environ["APP_ENV"] = "test"
