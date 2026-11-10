#!/bin/bash
# Remove scaffold symlinks from a target repository.
#
# Only removes symlinks that point into this scaffold repo (either via the
# in-container mount path or via $SCAFFOLD_DIR directly, matching install.sh's
# two modes). Non-symlink files are never touched.
#
# Usage: ./uninstall.sh /path/to/target_repo
set -euo pipefail

SCAFFOLD_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET_DIR="${1:?Usage: ./uninstall.sh /path/to/target_repo}"

# Derive scaffold name and in-container mount path the same way install.sh
# does, so uninstall recognises symlinks created by either mode without
# being told which one was used.
SCAFFOLD_NAME="$(basename "$SCAFFOLD_DIR")"
CONTAINER_SCAFFOLD="/opt/$SCAFFOLD_NAME"

if [ ! -d "$TARGET_DIR" ]; then
    echo "Error: $TARGET_DIR is not a directory"
    exit 1
fi

TARGET_DIR="$(cd "$TARGET_DIR" && pwd)"

echo "Uninstalling $SCAFFOLD_NAME scaffold"
echo "  Scaffold: $SCAFFOLD_DIR"
echo "  Target:   $TARGET_DIR"
echo ""

# --- Helper: remove a symlink only if it points into the scaffold ---
# Accepts symlinks created in any install mode: host-mode symlinks point at
# $CONTAINER_SCAFFOLD/<rel_path>, in_container-mode symlinks at
# $SCAFFOLD_DIR/<rel_path>, and --pin symlinks at the target's own pinned
# copy by a relative path (scripts/spend.py -> ../.prism/scripts/spend.py).
# Any other target is left alone to avoid removing user-managed symlinks
# that happen to share a relative path.
PIN_DIR="$TARGET_DIR/.$SCAFFOLD_NAME"
unlink_file() {
    local rel_path="$1"
    local dst="$TARGET_DIR/$rel_path"
    local up="" slashes="${rel_path//[^\/]/}" i
    for ((i = 0; i < ${#slashes}; i++)); do up="../$up"; done

    if [ -L "$dst" ]; then
        local link_target
        link_target="$(readlink "$dst")"
        if [ "$link_target" = "$CONTAINER_SCAFFOLD/$rel_path" ] \
           || [ "$link_target" = "$SCAFFOLD_DIR/$rel_path" ] \
           || [ "$link_target" = "$up.$SCAFFOLD_NAME/$rel_path" ]; then
            rm "$dst"
            echo "  Removed: $rel_path"
        else
            echo "  SKIP (points elsewhere: $link_target): $rel_path"
        fi
    elif [ -e "$dst" ]; then
        echo "  SKIP (not a symlink): $rel_path"
    fi
}

# --- Helper: reverse directory localizations recorded by install.sh ---
# For each promoted symlink recorded in the marker file, attempt to restore
# the original symlink. Reversal only proceeds when the promoted directory
# contains nothing but per-item symlinks pointing back to the original
# upstream — any extras (user additions, leftover scaffold content) leave
# the localization in place. The directory is still usable either way;
# a skipped reversal just means the target keeps the per-item mirror shape
# rather than the single-symlink shape.
reverse_localizations() {
    local marker="$TARGET_DIR/.scaffold-localizations"
    [ -f "$marker" ] || return 0

    echo ""
    echo "Reversing directory localizations..."

    local path upstream entry name safe
    # Read deepest-first so inner promotions are unwound before their outer
    # parents (install appends in outer-first order; tac reverses).
    while IFS=$'\t' read -r path upstream; do
        if [ -L "$path" ] || [ ! -d "$path" ]; then
            echo "  SKIP (not a real dir): ${path#$TARGET_DIR/}"
            continue
        fi
        if [ ! -d "$upstream" ]; then
            echo "  SKIP (upstream missing): ${path#$TARGET_DIR/}"
            continue
        fi

        safe=1
        shopt -s dotglob nullglob
        for entry in "$path"/*; do
            name="$(basename "$entry")"
            if [ ! -L "$entry" ] || [ "$(readlink "$entry")" != "$upstream/$name" ]; then
                safe=0
                break
            fi
        done
        shopt -u dotglob nullglob

        if [ "$safe" -eq 1 ]; then
            rm -rf "$path"
            ln -s "$upstream" "$path"
            echo "  Restored symlink: ${path#$TARGET_DIR/} -> $upstream"
        else
            echo "  SKIP (extra content): ${path#$TARGET_DIR/}"
        fi
    done < <(tac "$marker")

    rm -f "$marker"
}

echo "Removing symlinks..."

# scripts
unlink_file "scripts/dump_transcripts.py"
unlink_file "scripts/execute_evals.py"
unlink_file "scripts/launch_subagent.py"
unlink_file "scripts/spend.py"
unlink_file "scripts/lint_log.py"
unlink_file "scripts/openrouter_endpoints.py"
unlink_file "scripts/ledger_to_json.py"
unlink_file "scripts/check_report.py"
unlink_file "scripts/check_blinding.py"
unlink_file "scripts/check_keys.py"
unlink_file "scripts/new_ids.py"
unlink_file "scripts/headroom.py"
unlink_file "scripts/run_status.py"
unlink_file "scripts/prism_version.py"

# subagents
unlink_file "subagents/__init__.py"
unlink_file "subagents/cli.py"
unlink_file "subagents/runner.py"

unlink_file "subagents/environment_explorer/__init__.py"
unlink_file "subagents/environment_explorer/agent.py"
unlink_file "subagents/environment_explorer/main.py"
unlink_file "subagents/environment_explorer/system_prompt.py"

unlink_file "subagents/experiment_executor/__init__.py"
unlink_file "subagents/experiment_executor/agent.py"
unlink_file "subagents/experiment_executor/main.py"
unlink_file "subagents/experiment_executor/system_prompt.py"

unlink_file "subagents/transcript_analyst/__init__.py"
unlink_file "subagents/transcript_analyst/agent.py"
unlink_file "subagents/transcript_analyst/forensic_checks.py"
unlink_file "subagents/transcript_analyst/main.py"
unlink_file "subagents/transcript_analyst/system_prompt.py"

unlink_file "subagents/environment_mapper/__init__.py"
unlink_file "subagents/environment_mapper/agent.py"
unlink_file "subagents/environment_mapper/main.py"
unlink_file "subagents/environment_mapper/system_prompt.py"

unlink_file "subagents/variant_designer/__init__.py"
unlink_file "subagents/variant_designer/agent.py"
unlink_file "subagents/variant_designer/main.py"
unlink_file "subagents/variant_designer/system_prompt.py"

unlink_file "subagents/redteam_critic/__init__.py"
unlink_file "subagents/redteam_critic/agent.py"
unlink_file "subagents/redteam_critic/main.py"
unlink_file "subagents/redteam_critic/system_prompt.py"

# .claude/docs
unlink_file ".claude/docs/investigator/orchestrator_analyst_guide.md"
unlink_file ".claude/docs/contracts/analyst_forensic_addendum.md"
unlink_file ".claude/docs/contracts/analyst_interface_contract.md"
unlink_file ".claude/docs/contracts/critic_interface_contract.md"
unlink_file ".claude/docs/contracts/designer_interface_contract.md"
unlink_file ".claude/docs/investigator/diagnostician_analyst_guide.md"
unlink_file ".claude/docs/investigator/diagnostician_responsibilities.md"
unlink_file ".claude/docs/investigator/eval_science_principles.md"
unlink_file ".claude/docs/contracts/executor_interface_contract.md"
unlink_file ".claude/docs/contracts/explorer_interface_contract.md"
unlink_file ".claude/docs/references/inspect_reference.md"
unlink_file ".claude/docs/contracts/mapper_interface_contract.md"
unlink_file ".claude/docs/investigator/model_forensics_principles.md"
unlink_file ".claude/docs/investigator/orchestrator_responsibilities.md"
unlink_file ".claude/docs/references/scout_reference.md"
unlink_file ".claude/docs/investigator/subagent_invocation.md"
unlink_file ".claude/docs/pipelines.json"

# .claude/skills/orchestrate
unlink_file ".claude/skills/orchestrate/SKILL.md"
unlink_file ".claude/skills/orchestrate/experimental-design-patterns.md"
unlink_file ".claude/skills/orchestrate/hypothesis-methodology.md"

# .claude/skills/diagnose
unlink_file ".claude/skills/diagnose/SKILL.md"
unlink_file ".claude/skills/diagnose/candidate-methodology.md"
unlink_file ".claude/skills/diagnose/perturbation-design-patterns.md"

# .claude/settings.json
unlink_file ".claude/settings.json"

# --- Remove the pinned copy (install.sh --pin) ---
# Only a folder carrying install's marker file, so a user's own folder of the
# same name is never deleted.
if [ -f "$PIN_DIR/PINNED_COMMIT" ]; then
    rm -rf "$PIN_DIR"
    echo "  Removed pinned copy: .$SCAFFOLD_NAME/"
fi

# --- Restore devcontainer.json from backup ---
DEVCONTAINER="$TARGET_DIR/.devcontainer/devcontainer.json"
if [ -f "$DEVCONTAINER.pre-scaffold" ]; then
    echo ""
    echo "Restoring devcontainer.json..."
    mv "$DEVCONTAINER.pre-scaffold" "$DEVCONTAINER"
    echo "  Restored: devcontainer.json (from backup)"
fi

# --- Restore .gitignore from backup ---
if [ -f "$TARGET_DIR/.gitignore.pre-scaffold" ]; then
    echo ""
    echo "Restoring .gitignore..."
    mv "$TARGET_DIR/.gitignore.pre-scaffold" "$TARGET_DIR/.gitignore"
    echo "  Restored: .gitignore (from backup)"
fi


# --- Clean up empty directories left by symlink removal ---
echo ""
echo "Cleaning up empty directories..."
for dir in \
    "$TARGET_DIR/subagents/environment_explorer" \
    "$TARGET_DIR/subagents/experiment_executor" \
    "$TARGET_DIR/subagents/transcript_analyst" \
    "$TARGET_DIR/subagents/environment_mapper" \
    "$TARGET_DIR/subagents/variant_designer" \
    "$TARGET_DIR/subagents/redteam_critic" \
    "$TARGET_DIR/subagents" \
    "$TARGET_DIR/.claude/docs/investigator" \
    "$TARGET_DIR/.claude/docs/contracts" \
    "$TARGET_DIR/.claude/docs/references" \
    "$TARGET_DIR/.claude/docs" \
    "$TARGET_DIR/.claude/skills/orchestrate" \
    "$TARGET_DIR/.claude/skills/diagnose" \
    "$TARGET_DIR/.claude/skills"; do
    if [ -d "$dir" ] && [ -z "$(ls -A "$dir")" ]; then
        rmdir "$dir"
        echo "  Removed empty directory: ${dir#$TARGET_DIR/}"
    fi
done

# --- Reverse directory localizations (promoted symlinks) ---
reverse_localizations

# --- Undo assume-unchanged on restored files ---
echo ""
echo "Restoring git tracking on patched files..."
if [ -d "$TARGET_DIR/.git" ]; then
    git -C "$TARGET_DIR" update-index --no-assume-unchanged \
        .devcontainer/devcontainer.json \
        .gitignore \
        2>/dev/null || true
    echo "  Done"
fi

echo ""
echo "Uninstallation complete."
