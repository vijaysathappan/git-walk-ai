"""Dependency-aware migration waves and weighted coverage."""

from dataclasses import replace

from .effort import effort_class, estimate


class MigrationPlanner:
    def coverage(self, units: list) -> dict:
        total = sum(unit.weight for unit in units) or 1
        result = {}
        for mode in ("AUTO_MIGRATABLE", "ASSISTED_MIGRATION", "MANUAL_REENGINEERING",
                     "RETAIN_IN_EXCEL", "UNSUPPORTED", "RETIRE"):
            result[mode] = round(sum(unit.weight for unit in units if unit.mode == mode) / total * 100, 2)
        return result

    def waves(self, units: list, features: dict, blockers: list) -> tuple[list, list[dict]]:
        sheet_wave = {}
        unit_waves = {}
        for unit in units:
            wave = self._base_wave(unit)
            unit_waves[unit.unit_id] = wave
            if unit.source_type == "SHEET" and unit.sheet_id:
                sheet_wave[unit.sheet_id] = wave
        # Formula sheet DEPENDS_ON precedent sheet: migrate precedent no later than formula sheet.
        for _ in range(max(1, len(sheet_wave))):
            changed = False
            for edge in features["sheet_dependencies"]:
                dependent = edge["source_sheet_id"]
                precedent = edge["target_sheet_id"]
                if dependent in sheet_wave and precedent in sheet_wave and sheet_wave[precedent] > sheet_wave[dependent]:
                    sheet_wave[dependent] = min(5, sheet_wave[precedent] + 1); changed = True
            if not changed: break
        updated = []
        for unit in units:
            wave = sheet_wave.get(unit.sheet_id, unit_waves[unit.unit_id])
            if unit.mode in {"UNSUPPORTED", "RETAIN_IN_EXCEL"}:
                wave = 5
            updated.append(replace(unit, wave=wave))
        names = {0: ("Foundation", "Resolve ownership, identity, controls, repositories, and connectors."),
                 1: ("Structured data", "Migrate reference data, tables, names, and low-risk inputs."),
                 2: ("Calculation domains", "Translate formulas and dependency-ordered calculation services."),
                 3: ("Automation & integration", "Reengineer Power Query, VBA, jobs, and external interfaces."),
                 4: ("Workflow & controls", "Activate approvals, policy, audit, and operational controls."),
                 5: ("Experience & retirement", "Move reports and user journeys, then retire approved Excel surfaces.")}
        waves = []
        for number in range(6):
            members = [unit for unit in updated if unit.wave == number]
            if not members: continue
            points = sum(unit.weight for unit in members)
            waves.append({"wave_number": number, "name": names[number][0], "objective": names[number][1],
                          "unit_ids": [unit.unit_id for unit in members], "unit_count": len(members),
                          "effort_class": effort_class(points),
                          "depends_on": [previous["wave_number"] for previous in waves[-1:]]})
        return updated, waves

    @staticmethod
    def _base_wave(unit) -> int:
        if unit.source_type in {"CONNECTION", "EXTERNAL_LINK"}: return 0
        if unit.source_type in {"TABLE", "NAMED_RANGE"}: return 1
        if unit.source_type in {"FORMULA_PATTERN", "DOMAIN"}: return 2
        if unit.source_type in {"VBA_PROJECT", "POWER_QUERY"}: return 3
        if unit.source_type in {"CONTROL", "VALIDATION"}: return 4
        return 5

    def effort(self, units: list, blockers: list, validation_count: int) -> dict:
        return estimate(units, blockers, validation_count)
