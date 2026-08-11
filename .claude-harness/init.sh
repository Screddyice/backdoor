#!/bin/bash
# Development Environment Initializer (v3.0)

echo "=== Dev Environment Setup (v3.0 Memory Architecture) ==="
echo "Working directory: $(pwd)"

# Check we are in the right place
if [ ! -f "CLAUDE.md" ]; then
    echo "ERROR: Not in project root directory"
    exit 1
fi

# Show recent git history
echo ""
echo "=== Recent Git History ==="
git log --oneline -5 2>/dev/null || echo "Not a git repo yet"

# Show memory status
echo ""
echo "=== Memory Layers Status ==="

# Working context
if [ -f ".claude-harness/memory/working/context.json" ]; then
    computed=$(grep -o '"computedAt"[[:space:]]*:[[:space:]]*"[^"]*"' .claude-harness/memory/working/context.json 2>/dev/null | cut -d'"' -f4)
    echo "Working Context: Last compiled $computed"
else
    echo "Working Context: Not initialized"
fi

# Memory bundle (OKF v0.1: one concept file per entry)
count_concepts() {
    find ".claude-harness/memory/$1" -maxdepth 1 -name '*.md' ! -name 'index.md' ! -name 'log.md' 2>/dev/null | wc -l | tr -d ' '
}
if [ -f ".claude-harness/memory/index.md" ]; then
    echo "Episodic Memory: $(count_concepts decisions) decisions recorded"
    echo "Procedural Memory: $(count_concepts failures) failures, $(count_concepts successes) successes recorded"
    echo "Learned Rules: $(count_concepts rules) rules"
else
    echo "Memory Bundle: Not initialized"
fi

# Check feature status
echo ""
echo "=== Features Status ==="
if [ -f ".claude-harness/features/active.json" ]; then
    pending=$(grep -c '"status"[[:space:]]*:[[:space:]]*"pending"' .claude-harness/features/active.json 2>/dev/null) || pending=0
    in_progress=$(grep -c '"status"[[:space:]]*:[[:space:]]*"in_progress"' .claude-harness/features/active.json 2>/dev/null) || in_progress=0
    needs_tests=$(grep -c '"status"[[:space:]]*:[[:space:]]*"needs_tests"' .claude-harness/features/active.json 2>/dev/null) || needs_tests=0
    echo "Pending: $pending | In Progress: $in_progress | Needs Tests: $needs_tests"
else
    echo "No features file found"
fi

# Archived features
if [ -f ".claude-harness/features/archive.json" ]; then
    archived=$(grep -c '"id":' .claude-harness/features/archive.json 2>/dev/null) || archived=0
    echo "Archived: $archived completed features"
fi

# Loop state
echo ""
echo "=== Agentic Loop State ==="
if [ -f ".claude-harness/loops/state.json" ]; then
    status=$(grep -o '"status"[[:space:]]*:[[:space:]]*"[^"]*"' .claude-harness/loops/state.json 2>/dev/null | cut -d'"' -f4)
    feature=$(grep -o '"feature"[[:space:]]*:[[:space:]]*"[^"]*"' .claude-harness/loops/state.json 2>/dev/null | cut -d'"' -f4)
    looptype=$(grep -o '"type"[[:space:]]*:[[:space:]]*"[^"]*"' .claude-harness/loops/state.json 2>/dev/null | cut -d'"' -f4)
    linkedFeature=$(grep -o '"featureId"[[:space:]]*:[[:space:]]*"[^"]*"' .claude-harness/loops/state.json 2>/dev/null | head -1 | cut -d'"' -f4)
    if [ "$status" != "idle" ] && [ -n "$feature" ]; then
        attempt=$(grep -o '"attempt"[[:space:]]*:[[:space:]]*[0-9]*' .claude-harness/loops/state.json 2>/dev/null | grep -o '[0-9]*')
        if [ "$looptype" = "fix" ]; then
            echo "ACTIVE FIX: $feature (attempt $attempt, status: $status)"
            echo "Linked to: $linkedFeature"
            echo "Resume with: /claude-harness:flow $feature"
        else
            echo "ACTIVE LOOP: $feature (attempt $attempt, status: $status)"
            echo "Resume with: /claude-harness:flow $feature"
        fi
    else
        echo "No active loop"
    fi
fi

# Pending fixes
if [ -f ".claude-harness/features/active.json" ]; then
    pendingFixes=$(grep -c '"type"[[:space:]]*:[[:space:]]*"bugfix"' .claude-harness/features/active.json 2>/dev/null) || pendingFixes=0
    if [ "$pendingFixes" != "0" ]; then
        echo ""
        echo "Pending fixes: $pendingFixes"
    fi
fi

# Orchestration state
echo ""
echo "=== Orchestration State ==="
if [ -f ".claude-harness/agents/context.json" ]; then
    session=$(grep -o '"activeFeature"[[:space:]]*:[[:space:]]*"[^"]*"' .claude-harness/agents/context.json 2>/dev/null | cut -d'"' -f4)
    if [ -n "$session" ]; then
        echo "Active orchestration: $session"
        echo "Run /claude-harness:flow to resume"
    else
        echo "No active orchestration"
    fi
else
    echo "No orchestration context yet"
fi

echo ""
echo "=== Environment Ready ==="
echo "Skills (6 total):"
echo "  /claude-harness:setup          - Initialize harness (one-time)"
echo "  /claude-harness:start          - Compile context, show GitHub dashboard"
echo "  /claude-harness:flow           - Unified workflow (recommended)"
echo "  /claude-harness:checkpoint     - Save progress, persist memory"
echo "  /claude-harness:merge          - Merge PRs, close issues"
echo "  /claude-harness:prd-breakdown  - Break PRD into features"
echo "  Flags: --no-merge --plan-only --autonomous --quick --fix --team"
