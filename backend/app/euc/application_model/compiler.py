"""Deterministic scaffold compiler for the first Stage 2.5 target profile."""

from __future__ import annotations

import io
import json
import re
import zipfile
from typing import Any


TYPE_MAP = {
    "STRING": "TEXT", "IDENTIFIER": "VARCHAR(160)", "INTEGER": "BIGINT",
    "DECIMAL": "NUMERIC(18,6)", "MONEY": "NUMERIC(18,4)",
    "PERCENTAGE": "NUMERIC(10,8)", "BOOLEAN": "BOOLEAN",
    "DATE": "DATE", "DATETIME": "TIMESTAMPTZ", "TIME": "TIME",
    "ENUM": "TEXT", "JSON": "JSONB", "BINARY": "BYTEA",
}


def _safe(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", str(value).lower()).strip("_") or "item"


def _title(value: str) -> str:
    return "".join(part.title() for part in _safe(value).split("_"))


class ScaffoldCompiler:
    profile = "WEB_POSTGRES_FASTAPI_REACT"

    def compile(self, air: dict, rejected: set[str] | None = None) -> dict[str, bytes]:
        rejected = rejected or set()
        entities = [item for item in air.get("entities", []) if item["component_id"] not in rejected]
        apis = [item for item in air.get("apis", []) if item["component_id"] not in rejected]
        screens = [item for item in air.get("screens", []) if item["component_id"] not in rejected]
        tests = [item for item in air.get("tests", []) if item["component_id"] not in rejected]
        provenance = {item["component_id"]: item["provenance"] for key, values in air.items()
                      if isinstance(values, list) for item in values
                      if isinstance(item, dict) and item.get("component_id") and item.get("provenance")}
        files: dict[str, str] = {
            "application.json": json.dumps({
                "name": air["application"]["name"], "air_version": air["air_version"],
                "target_profile": air["target_profile"], "source_manifest": "../provenance.json",
            }, indent=2, sort_keys=True),
            "provenance.json": json.dumps(provenance, indent=2, sort_keys=True),
            "README.md": self._readme(air),
            "database/schema/001_initial.sql": self._sql(entities),
            "backend/app/__init__.py": "",
            "backend/app/api/__init__.py": "",
            "backend/app/services/__init__.py": "",
            "backend/app/integrations/__init__.py": "",
            "backend/app/workflows/__init__.py": "",
            "backend/app/main.py": self._backend_main(air),
            "backend/app/models.py": self._models(entities),
            "backend/app/api/generated.py": self._api(apis, entities),
            "backend/app/services/calculations.py": self._calculations(air.get("calculations", []), rejected),
            "backend/app/integrations/contracts.py": self._integrations(air.get("integrations", []), rejected),
            "backend/app/workflows/definitions.py": self._workflows(air.get("workflows", []), rejected),
            "backend/requirements.txt": "fastapi>=0.110\nuvicorn>=0.29\nsqlalchemy>=2.0\npsycopg[binary]>=3.1\npydantic>=2.6\n",
            "frontend/package.json": json.dumps({"name": _safe(air["application"]["name"]), "private": True,
                                                   "scripts": {"dev": "vite", "build": "tsc && vite build"},
                                                   "dependencies": {"@vitejs/plugin-react": "latest", "vite": "latest",
                                                                    "typescript": "latest", "react": "latest", "react-dom": "latest"},
                                                   "devDependencies": {}}, indent=2, sort_keys=True),
            "frontend/index.html": "<div id=\"root\"></div><script type=\"module\" src=\"/src/main.tsx\"></script>\n",
            "frontend/tsconfig.json": json.dumps({"compilerOptions": {"target": "ES2022", "module": "ESNext",
                "moduleResolution": "Bundler", "jsx": "react-jsx", "strict": True,
                "skipLibCheck": True, "noEmit": True}, "include": ["src"]}, indent=2, sort_keys=True),
            "frontend/vite.config.ts": "import { defineConfig } from 'vite';\nimport react from '@vitejs/plugin-react';\nexport default defineConfig({ plugins: [react()] });\n",
            "frontend/src/App.tsx": self._react(screens),
            "frontend/src/main.tsx": "import React from 'react';\nimport { createRoot } from 'react-dom/client';\nimport App from './App';\nimport './styles.css';\ncreateRoot(document.getElementById('root')!).render(<App />);\n",
            "frontend/src/styles.css": self._styles(),
            "tests/parity/test_generated_parity.py": self._tests(tests),
            "deployment/.env.example": "DATABASE_URL=postgresql+psycopg://user:password@localhost/application\n",
        }
        return {path: content.encode("utf-8") for path, content in sorted(files.items())}

    @staticmethod
    def bundle(files: dict[str, bytes]) -> bytes:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for path, payload in sorted(files.items()):
                info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                archive.writestr(info, payload)
        return output.getvalue()

    @staticmethod
    def _header(component_id: str = "AIR_ROOT") -> str:
        return f"# Generated by Git Walk generator@2.5.0; provenance: {component_id}\n"

    def _readme(self, air):
        return (f"# {air['application']['name']}\n\nGenerated from immutable AIR `{air['air_version']}`.\n\n"
                "Generated files may be regenerated. Put custom behavior behind extension interfaces; do not add secrets to this project.\n")

    def _sql(self, entities):
        blocks = ["-- Generated schema. Review constraints before production migration.\n"]
        for entity in entities:
            if not entity.get("persistent"):
                continue
            columns = []
            for field in entity.get("fields", []):
                value = f"    {_safe(field['name'])} {TYPE_MAP.get(field['type'], 'TEXT')}"
                if field.get("confirmed_primary_key"):
                    value += " PRIMARY KEY"
                elif not field.get("nullable", True):
                    value += " NOT NULL"
                columns.append(value)
            columns.append("    gitwalk_version BIGINT NOT NULL DEFAULT 0")
            blocks.append(f"-- provenance: {entity['component_id']}\nCREATE TABLE {_safe(entity['entity_key'])} (\n" + ",\n".join(columns) + "\n);\n")
        return "\n".join(blocks)

    def _backend_main(self, air):
        return self._header() + "from fastapi import FastAPI\nfrom .api.generated import router\n\napp = FastAPI(title=" + repr(air["application"]["name"]) + ")\napp.include_router(router, prefix='/api/v1')\n"

    def _models(self, entities):
        lines = [self._header(), "from pydantic import BaseModel\n\n"]
        pytypes = {"INTEGER": "int", "DECIMAL": "float", "MONEY": "float", "PERCENTAGE": "float", "BOOLEAN": "bool"}
        for entity in entities:
            lines.append(f"# provenance: {entity['component_id']}\nclass {_title(entity['entity_key'])}(BaseModel):\n")
            for field in entity.get("fields", []):
                typename = pytypes.get(field["type"], "str")
                lines.append(f"    {_safe(field['name'])}: {typename}{' | None = None' if field.get('nullable', True) else ''}\n")
            if not entity.get("fields"):
                lines.append("    pass\n")
            lines.append("\n")
        return "".join(lines)

    def _api(self, apis, entities):
        entity_names = {item["component_id"]: _title(item["entity_key"]) for item in entities}
        lines = [self._header(), "from fastapi import APIRouter, HTTPException\nfrom .. import models\n\nrouter = APIRouter()\n\n"]
        for index, endpoint in enumerate(apis):
            model = entity_names.get(endpoint.get("entity_id"), "dict")
            method = endpoint["method"].lower()
            path = endpoint["path"]
            function_name = f"{endpoint['operation']}_{_safe(model)}_{index}"
            parameters = []
            if "{id}" in path:
                parameters.append("id: str")
            if endpoint["method"] in {"POST", "PUT"} and model != "dict":
                parameters.append(f"payload: models.{model}")
            lines.append(f"# provenance: {endpoint['component_id']}\n@router.{method}({path!r})\nasync def {function_name}({', '.join(parameters)}):\n    raise HTTPException(status_code=501, detail='Generated contract requires a repository implementation')\n\n")
        return "".join(lines)

    def _calculations(self, calculations, rejected):
        lines = [self._header(), "\n"]
        for item in calculations:
            if item["component_id"] in rejected:
                continue
            lines.append(f"# provenance: {item['component_id']}\ndef {_safe(item['name'])}(context: dict):\n")
            if item.get("translation_category") == "DIRECT_TRANSLATION":
                lines.append(f"    # Source expression: {item.get('normalized_formula', '')}\n    raise NotImplementedError('Bind semantic inputs before enabling this calculation')\n\n")
            else:
                lines.append(f"    raise NotImplementedError('Review required: {item.get('translation_category')}')\n\n")
        return "".join(lines)

    def _integrations(self, integrations, rejected):
        lines = [self._header(), "from typing import Protocol\n\n"]
        for item in integrations:
            if item["component_id"] not in rejected:
                lines.append(f"# provenance: {item['component_id']}\nclass {_title(item['name'])}Adapter(Protocol):\n    def fetch(self) -> list[dict]: ...\n\n")
        return "".join(lines)

    def _workflows(self, workflows, rejected):
        definitions = [{"id": item["component_id"], "name": item["name"], "states": item["states"], "transitions": item["transitions"]}
                       for item in workflows if item["component_id"] not in rejected]
        return self._header() + "WORKFLOWS = " + repr(definitions) + "\n"

    def _react(self, screens):
        cards = "\n".join(f"        <article><span>{screen['screen_type'].replace('_', ' ')}</span><h2>{screen['name']}</h2><p>Source lineage {screen['component_id']}</p></article>" for screen in screens)
        return "import React from 'react';\n\nexport default function App() {\n  return <main><header><b>Native application</b><h1>Generated workspace</h1></header><section>\n" + cards + "\n      </section></main>;\n}\n"

    @staticmethod
    def _styles():
        return "@import url('https://fonts.googleapis.com/css2?family=Manrope:wght@400;700&display=swap');\n:root{font-family:Manrope,sans-serif;color:#13221c;background:#edf2ed}body{margin:0}main{padding:4vw}header{padding:3rem;border-radius:24px;color:white;background:linear-gradient(120deg,#101820,#175b42)}section{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:1rem;margin-top:1rem}article{padding:1.5rem;border:1px solid #c9d5cd;border-radius:18px;background:white}span{color:#16825d;font-size:.75rem;font-weight:700;text-transform:uppercase}\n"

    def _tests(self, tests):
        lines = [self._header(), "import pytest\n\n"]
        for index, item in enumerate(tests):
            lines.append(f"# provenance: {item['component_id']}\n@pytest.mark.skip(reason='Bind captured EUC inputs and expected outputs')\ndef test_parity_{index}():\n    assert True\n\n")
        if not tests:
            lines.append("@pytest.mark.skip(reason='No Stage 2.4 validation evidence was available')\ndef test_parity_plan_required():\n    assert True\n")
        return "".join(lines)
