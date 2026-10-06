"""Table tests for the deterministic runtime classifier."""

import json

import pytest

from code_analysis.domain.services.runtime_classifier import (
    EMPTY_PROFILE,
    ProjectProfile,
    Runtime,
    build_project_profile,
    classify,
    classify_values,
)


def _pkg(deps: list[str]) -> str:
    return json.dumps({"dependencies": {d: "1.0.0" for d in deps}})


@pytest.mark.parametrize(
    "path,content,expected",
    [
        # Browser
        ("src/components/Checkout.tsx", "import React from 'react'\n", ["browser"]),
        ("src/store.ts", "const t = localStorage.getItem('token')\n", ["browser"]),
        ("src/env.ts", "const k = import.meta.env.VITE_API_KEY\n", ["browser"]),
        ("public/index.html", "<html></html>", ["browser"]),
        ("src/App.vue", "<template></template>", ["browser"]),
        # Server
        (
            "src/app/task/task.controller.ts",
            "import { Controller } from '@nestjs/common'\n",
            ["server"],
        ),
        ("src/server.js", "const express = require('express')\n", ["server"]),
        ("app/main.py", "from fastapi import FastAPI\n", ["server"]),
        ("app/util.py", "def helper():\n    pass\n", ["server"]),
        ("cmd/main.go", 'import "net/http"\n', ["server"]),
        ("db/migrations/001.sql", "CREATE TABLE users ();", ["server"]),
        # Mobile
        ("app/screens/Login.tsx", "import { View } from 'react-native'\n", ["mobile"]),
        ("android/app/src/main/AndroidManifest.xml", "<manifest/>", ["mobile"]),
        ("ios/App/Info.plist", "<plist/>", ["mobile"]),
        ("ios/App/Auth.swift", "import Foundation", ["mobile"]),
        ("lib/main.dart", "void main() {}", ["mobile"]),
        ("pubspec.yaml", "name: app", ["mobile"]),
        (
            "app/src/main/java/com/example/Auth.kt",
            "import android.content.Context\n",
            ["mobile"],
        ),
        (
            "android/app/build.gradle",
            "apply plugin: 'com.android.application'",
            ["mobile", "config"],
        ),
        ("app.json", '{"expo": {}}', ["mobile", "config"]),
        # Both browser and server signals
        (
            "src/ssr.ts",
            "import React from 'react'\nimport express from 'express'\n",
            ["browser", "server"],
        ),
        ("views/index.ejs", "<%= user %>", ["browser", "server"]),
        # Infra
        ("aws/batch/terragrunt.hcl", "terraform {}", ["infra"]),
        (".github/workflows/deploy.yml", "on: push", ["infra"]),
        ("Dockerfile", "FROM python:3.13", ["infra"]),
        ("scripts/deploy.sh", "#!/bin/bash", ["infra"]),
        (".github/scripts/release.ts", "import React from 'react'", ["infra"]),
        # Config
        ("package.json", "{}", ["config"]),
        (".env.production", "KEY=1", ["config"]),
        ("README.md", "# Title", ["config"]),
        ("pom.xml", "<project/>", ["config"]),
        # Test
        ("tests/unit/test_task_service.py", "from flask import Flask\n", ["test"]),
        ("src/app.spec.ts", "describe()", ["test"]),
        ("src/__tests__/App.tsx", "import React from 'react'", ["test"]),
        ("pkg/handler_test.go", "package pkg", ["test"]),
        # Unknown
        (
            "src/utils/format.ts",
            "export const f = (x: string) => x.trim()\n",
            ["unknown"],
        ),
        ("data/notes.ipynb", "{}", ["unknown"]),
    ],
)
def test_classify_table(path, content, expected):
    assert classify_values(path, content) == expected


def test_classify_is_pure():
    args = ("src/a.ts", "import React from 'react'\nconst x = express()\n")
    assert classify(*args) == classify(*args)


def test_directory_tie_break_for_js_without_signals():
    assert classify_values("frontend/src/helpers/money.ts", "export const a = 1") == [
        "browser"
    ]
    assert classify_values("backend/src/helpers/money.ts", "export const a = 1") == [
        "server"
    ]


class TestProjectProfile:
    def test_empty_profile_without_manifest(self):
        profile = build_project_profile(
            [
                {
                    "path": "src/components/Card.tsx",
                    "content": "import React from 'react'",
                }
            ]
        )
        assert profile.is_empty

    def test_mobile_js_profile(self):
        profile = build_project_profile(
            [{"path": "package.json", "content": _pkg(["react", "react-native"])}]
        )
        assert profile.mobile_js is True
        assert profile.frontend_js is False

    def test_frontend_profile(self):
        profile = build_project_profile(
            [{"path": "package.json", "content": _pkg(["react", "vite"])}]
        )
        assert profile.frontend_js is True
        assert profile.backend_js is False

    def test_backend_profile(self):
        profile = build_project_profile(
            [{"path": "package.json", "content": _pkg(["@nestjs/core"])}]
        )
        assert profile.backend_js is True

    def test_fullstack_profile_is_neutral(self):
        profile = build_project_profile(
            [{"path": "package.json", "content": _pkg(["react", "express"])}]
        )
        assert profile.frontend_js is False
        assert profile.backend_js is False

    def test_invalid_package_json_is_ignored(self):
        profile = build_project_profile([{"path": "package.json", "content": "{oops"}])
        assert profile.is_empty

    def test_mobile_profile_adds_mobile_to_tsx_without_import(self):
        profile = ProjectProfile(mobile_js=True)
        runtimes = classify_values(
            "src/components/Card.tsx", "import React from 'react'\n", profile
        )
        assert runtimes == ["mobile", "browser"]

    def test_frontend_profile_defaults_ambiguous_file_to_browser(self):
        profile = ProjectProfile(frontend_js=True)
        assert classify_values("src/api/client.ts", "export const a = 1", profile) == [
            "browser",
            "unknown",
        ]

    def test_backend_profile_defaults_ambiguous_file_to_server(self):
        profile = ProjectProfile(backend_js=True)
        assert classify_values("src/lib/money.ts", "export const a = 1", profile) == [
            "server",
            "unknown",
        ]

    def test_profile_never_removes_runtimes(self):
        profile = ProjectProfile(backend_js=True)
        assert classify_values("src/ui.tsx", "import React from 'react'", profile) == [
            "browser"
        ]

    def test_android_profile_widens_plain_kotlin(self):
        profile = ProjectProfile(android=True)
        assert classify_values("app/Util.kt", "fun a() = 1", profile) == ["mobile"]
        assert classify_values("app/Util.kt", "fun a() = 1", EMPTY_PROFILE) == [
            "server"
        ]

    def test_profile_to_dict(self):
        assert ProjectProfile(ios=True).to_dict()["ios"] is True


def test_runtime_enum_values_are_strings():
    assert Runtime.BROWSER == "browser"
    assert {r.value for r in Runtime} == {
        "browser",
        "server",
        "mobile",
        "infra",
        "test",
        "config",
        "unknown",
    }
