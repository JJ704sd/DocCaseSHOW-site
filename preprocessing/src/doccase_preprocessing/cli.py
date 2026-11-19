"""Local CLI: input JSON/EML -> originals, material manifest and parse task plan."""

import argparse
import json
from dataclasses import fields
from pathlib import Path

from .inputs import adapt_eml, adapt_request
from .models import InputError, Limits, ParserConfig
from .pipeline import prepare


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--task-id", help="Required for EML; JSON carries task_id")
    parser.add_argument("--email-ref-id", help="Required for EML; JSON carries email_ref_id")
    parser.add_argument(
        "--limits", type=Path, help="JSON object with explicit local Limits overrides"
    )
    parser.add_argument("--parser-version", default="unbound")
    parser.add_argument("--parser-options", type=Path, help="Downstream parser options JSON object")
    args = parser.parse_args()
    try:
        overrides = json.loads(args.limits.read_text(encoding="utf-8")) if args.limits else {}
        if not isinstance(overrides, dict) or set(overrides) - {f.name for f in fields(Limits)}:
            raise InputError("Limits must be an object with known limit names")
        try:
            limits = Limits(**overrides)
        except ValueError as error:
            raise InputError(str(error)) from error
        options = (
            json.loads(args.parser_options.read_text(encoding="utf-8"))
            if args.parser_options
            else {}
        )
        if not isinstance(options, dict):
            raise InputError("Parser options must be a JSON object")
        with args.input.open("rb") as stream:
            raw = stream.read(limits.max_input_bytes + 1)
        if len(raw) > limits.max_input_bytes:
            raise InputError(f"Input exceeds {limits.max_input_bytes} bytes")
        if args.input.suffix.lower() == ".eml":
            if not args.task_id or not args.email_ref_id:
                raise InputError("EML requires --task-id and --email-ref-id")
            bundle = adapt_eml(
                raw, task_id=args.task_id, email_ref_id=args.email_ref_id, limits=limits
            )
        elif args.input.suffix.lower() == ".json":
            bundle = adapt_request(json.loads(raw), limits)
        else:
            raise InputError("Input must be a .json request or .eml message")
        result = prepare(
            bundle,
            args.output,
            limits=limits,
            parser_config=ParserConfig(version=args.parser_version, options=options),
        )
    except (InputError, UnicodeError, json.JSONDecodeError) as error:
        parser.exit(2, f"Input error: {error}\n")
    print(
        json.dumps(
            {
                "state": result["state"],
                "manifest": str(args.output / result["manifest_path"]),
                "summary": result["summary"],
                "parsing_completed": False,
            },
            ensure_ascii=False,
        )
    )
    return 0 if result["state"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
