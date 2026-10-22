"""Keep route methods, paths, names, aliases and registration order stable."""

import json
import unittest
from pathlib import Path

import main


class RouteManifestTest(unittest.TestCase):
    def test_registered_routes_match_pre_split_manifest(self):
        expected = json.loads((Path(__file__).with_name("route_baseline.json")).read_text(
            encoding="utf-8"))

        def effective(routes):
            for route in routes:
                if hasattr(route, "path"):
                    yield route
                elif hasattr(route, "original_router"):
                    yield from effective(route.original_router.routes)

        actual = [
            [sorted(route.methods) if hasattr(route, "methods") else ["WS"],
             route.path, route.name]
            for route in effective(main.app.routes)
        ]
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
