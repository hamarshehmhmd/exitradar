import textwrap

from exitradar import feed, inventory


def w(tmp_path, rel, text):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(text))
    return p


def deps_of(tmp_path, eco=None):
    _, events = feed.load()
    ds = inventory.walk(str(tmp_path), feed.text_patterns(events))
    return [d for d in ds if eco is None or d.eco == eco]


def test_workflow_actions_images_and_local_actions(tmp_path):
    w(tmp_path, ".github/workflows/ci.yml", """
        jobs:
          t:
            container: { image: node:20 }
            services:
              db:
                image: "bitnami/postgresql:16"
            steps:
              - uses: actions/checkout@v5
              - uses: ./.github/actions/local
              - uses: github/codeql-action/init@v3   # sub-path action
              - uses: docker://alpine:3.20
              - name: x
                uses: 'actions-rs/cargo@v1'
    """)
    acts = {d.name: d for d in deps_of(tmp_path, "action")}
    assert set(acts) == {"actions/checkout", "github/codeql-action/init", "actions-rs/cargo"}
    assert acts["github/codeql-action/init"].github == "github/codeql-action"
    assert acts["actions/checkout"].version == "v5"
    assert acts["actions/checkout"].line == 9
    imgs = sorted(d.name for d in deps_of(tmp_path, "image"))
    assert imgs == ["alpine:3.20", "bitnami/postgresql:16"]


def test_dockerfile_skips_stage_names_args_and_scratch(tmp_path):
    w(tmp_path, "svc/Dockerfile", """
        ARG BASE=python:3.12
        FROM ${BASE} AS deps
        FROM --platform=linux/amd64 golang:1.23 AS build
        FROM build AS test
        FROM scratch
        FROM denoland/deno:2.1
    """)
    imgs = [d.name for d in deps_of(tmp_path, "image")]
    assert imgs == ["golang:1.23", "denoland/deno:2.1"]
    assert [d.name for d in deps_of(tmp_path, "runtime")] == ["deno"]


def test_chart_dependencies_only(tmp_path):
    w(tmp_path, "charts/app/Chart.yaml", """
        apiVersion: v2
        name: app
        version: 1.0.0
        dependencies:
          - name: redis
            version: 19.0.0
            repository: https://charts.bitnami.com/bitnami
          - name: local
            repository: file://../local
          - repository: oci://registry-1.docker.io/bitnamicharts
            name: postgresql
        maintainers:
          - name: someone
    """)
    helm = sorted(d.name for d in deps_of(tmp_path, "helm"))
    assert helm == ["https://charts.bitnami.com/bitnami/redis", "oci://registry-1.docker.io/bitnamicharts/postgresql"]


def test_helm_repo_add_in_scripts(tmp_path):
    w(tmp_path, ".github/workflows/deploy.yml", """
        steps:
          - run: helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
    """)
    assert [d.name for d in deps_of(tmp_path, "helm")] == ["https://kubernetes.github.io/ingress-nginx"]


def test_package_json_sections_and_github_refs(tmp_path):
    w(tmp_path, "package.json", """
        {"dependencies": {"request": "^2.88.0", "left": "github:someone/left-pad#v1"},
         "devDependencies": {"tslint": "6", "x": "someone/x"},
         "optionalDependencies": {"fsevents": "2"},
         "peerDependencies": {"react": "18"}}
    """)
    d = {x.name: x for x in deps_of(tmp_path, "npm")}
    assert set(d) == {"request", "left", "tslint", "x", "fsevents"}
    assert d["left"].github == "someone/left-pad"
    assert d["x"].github == "someone/x"
    assert d["request"].github == ""


def test_node_modules_and_venv_are_skipped(tmp_path):
    w(tmp_path, "node_modules/request/package.json", '{"dependencies": {"tslint": "1"}}')
    w(tmp_path, ".venv/lib/requirements.txt", "localstack\n")
    assert deps_of(tmp_path) == []


def test_deno_json_imports(tmp_path):
    w(tmp_path, "deno.jsonc", """
        // comment
        {"imports": {"chalk": "npm:chalk@5", "@std/path": "jsr:@std/path@^1", "x": "https://deno.land/x/y.ts"}}
    """)
    ds = deps_of(tmp_path)
    assert ("npm", "chalk") in [(d.eco, d.name) for d in ds]
    assert ("jsr", "@std/path") in [(d.eco, d.name) for d in ds]
    assert ("runtime", "deno") in [(d.eco, d.name) for d in ds]


def test_requirements_variants(tmp_path):
    w(tmp_path, "requirements-dev.txt", """
        # comment
        -r base.txt
        Django[argon2]>=4.2  ; python_version>"3.8"
        nose==1.3.7
        mylib @ git+https://github.com/acme/mylib.git@main
        --index-url https://x
    """)
    d = {x.name: x for x in deps_of(tmp_path, "pypi")}
    assert set(d) == {"Django", "nose", "mylib"}
    assert d["mylib"].github == "acme/mylib"
    assert d["nose"].line == 5


def test_pyproject_pep621_groups_and_poetry(tmp_path):
    w(tmp_path, "pyproject.toml", """
        [project]
        dependencies = ["requests>=2", "localstack-core"]
        [project.optional-dependencies]
        dev = ["pytest"]
        [dependency-groups]
        lint = ["ruff", {include-group = "dev"}]
        [tool.poetry.dependencies]
        python = "^3.11"
        moto = "^5"
        fork = { git = "https://github.com/acme/fork.git" }
    """)
    d = {x.name: x for x in deps_of(tmp_path, "pypi")}
    assert set(d) == {"requests", "localstack-core", "pytest", "ruff", "moto", "fork"}
    assert d["fork"].github == "acme/fork"


def test_go_mod_block_and_single(tmp_path):
    w(tmp_path, "go.mod", """
        module example.com/x
        go 1.23
        require github.com/pkg/errors v0.9.1
        require (
            github.com/golang/mock v1.6.0 // indirect
            golang.org/x/net v0.30.0
        )
    """)
    d = {x.name: x for x in deps_of(tmp_path, "go")}
    assert set(d) == {"github.com/pkg/errors", "github.com/golang/mock", "golang.org/x/net"}
    assert d["github.com/golang/mock"].github == "golang/mock"
    assert d["golang.org/x/net"].github == ""


def test_cargo_renames_paths_and_git(tmp_path):
    w(tmp_path, "Cargo.toml", """
        [package]
        name = "x"
        [dependencies]
        serde = "1"
        failure = { version = "0.1" }
        mine = { path = "../mine" }
        json = { package = "serde_json", version = "1" }
        [dev-dependencies]
        g = { git = "https://github.com/acme/g" }
    """)
    d = {x.name: x for x in deps_of(tmp_path, "crates")}
    assert set(d) == {"serde", "failure", "serde_json", "g"}
    assert d["g"].github == "acme/g"


def test_gemfile_and_terraform(tmp_path):
    w(tmp_path, "Gemfile", """
        source "https://rubygems.org"
        gem "htmlentities"
        gem 'forked', github: 'acme/forked'
    """)
    w(tmp_path, "infra/main.tf", """
        terraform { required_providers { aws = { source = "hashicorp/aws" } } }
        module "vpc" { source = "github.com/acme/tf-vpc?ref=v1" }
    """)
    g = {x.name: x for x in deps_of(tmp_path, "gems")}
    assert set(g) == {"htmlentities", "forked"} and g["forked"].github == "acme/forked"
    t = {x.name: x for x in deps_of(tmp_path, "terraform")}
    assert "hashicorp/aws" in t
    assert any(x.github == "acme/tf-vpc" for x in t.values())


def test_text_patterns_in_code_not_docs(tmp_path):
    w(tmp_path, "src/hook.ts", 'const u = "https://acme.web.val.run";\nimport x from "https://esm.town/v/me/x";\n')
    w(tmp_path, "README.md", "we used to deploy with deployctl\n")
    ds = deps_of(tmp_path, "text")
    assert [(d.name, d.file, d.line) for d in ds] == [("val-town-winddown", "src/hook.ts", 1)]
    assert ds[0].extra["match"] == "acme.web.val.run"
