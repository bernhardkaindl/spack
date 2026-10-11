#!/bin/bash
# validate-layers.sh MAPFILE OLDFILES NEWFILES LINTFILES
#
# Validates the last layer listed in MAPFILE ("OLDCOMMIT NEWCOMMIT" per line, oldest first). Its tree
# already contains every earlier layer, so the cumulative coverage is the same. Set VALIDATE_ALL=1 to
# check every listed layer. The map must still start at layer 1 so that BASE is correct.
# in scratch worktrees. OLDFILES/NEWFILES are the test files whose collected test IDs must match
# before and after the rewrite; LINTFILES are extra files for ruff (every .py file changed by the
# layer is always linted).
#
# Checks (select with CHECKS=comma,list; default is all):
#   ids      collected test IDs are identical (path stripped)
#   ruff     ruff check and ruff format --check
#   style    bin/spack style --base <parent layer> (import, ruff, mypy), as in the prechecks workflow;
#            catches redundant/missing `import spack.x` that ruff does not report
#   vermin   vermin -t=3.6- with the CI flags, plus a grep for 3.7+ keyword arguments in added lines
#   pylint   unspecified-encoding, as in the prechecks workflow
#   mypy     mypy on the changed files
#   license  spack license verify
#   slots    slotscheck, as in the prechecks workflow
#   cov      focused coverage run; diff-cover against the parent layer, gated per production file
#   tests    pytest -n 8 on NEWFILES
#
# Environment:
#   CHECKS         checks to run (RUN_TESTS=0 removes "tests" for compatibility)
#   MIN_COVERAGE   hard diff-coverage gate for every production file individually, default 85.26
#   MIN_TOTAL      hard diff-coverage gate for the layer as a whole, default 89
#   NS_REGEX       production files that also get the soft target, default sandbox*.py|installer/build.py
#   NS_TARGET      soft target for those files, default 90
#   VALIDATE_ALL   1 to validate every layer in MAPFILE, default 0 (last layer only)
#   COV_EXTRA      extra test paths for the coverage run (the test files the series changed are
#                  always included)
#
# The coverage run uses only the tests changed by the series, so it can under-report relative to the
# full suite; confirm a failing file with the full suite before adding tests.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../../../.." && pwd); PY=$REPO/.venv/bin/python; VENV=$REPO/.venv/bin
MAP=$1; OLD=($2); NEW=($3); LINT=($4)
CHECKS=${CHECKS:-ids,ruff,style,vermin,pylint,mypy,license,slots,cov,tests}
[ "${RUN_TESTS:-1}" = 0 ] && CHECKS=${CHECKS/,tests/}
MIN_COVERAGE=${MIN_COVERAGE:-85.26}
MIN_TOTAL=${MIN_TOTAL:-89}
NS_REGEX=${NS_REGEX:-'/(sandbox[^/]*|installer/build)\.py$'}
NS_TARGET=${NS_TARGET:-90}
COV_EXTRA=${COV_EXTRA:-}
want() { [[ ",$CHECKS," == *",$1,"* ]]; }

W=/tmp/tm-validate; rm -rf $W.old $W.new
git -C $REPO -c core.hooksPath=/dev/null worktree add -q --detach $W.old HEAD 2>/dev/null
git -C $REPO -c core.hooksPath=/dev/null worktree add -q --detach $W.new HEAD 2>/dev/null
clean_env() { env -u PYTHONSTARTUP -u PYTHON_BASIC_REPL "$@"; }
ids() { (cd $1 && clean_env PYTHONPATH=$1/lib/spack $PY -m pytest -p no:cacheprovider --collect-only -q "${@:2}" 2>/dev/null | grep '::' | sed 's/^.*:://' | sort); }

BASE=$(git -C $REPO rev-parse "$(head -1 "$MAP" | awk '{print $2}')^")
fail=0
LAYERS=$(mktemp)
if [ "${VALIDATE_ALL:-0}" = 1 ]; then cp "$MAP" "$LAYERS"; else tail -1 "$MAP" > "$LAYERS"; fi
n=$(( $(wc -l < "$MAP") - $(wc -l < "$LAYERS") ))
while read old new; do n=$((n+1))
  PARENT=$(git -C $REPO rev-parse "$new^")
  git -C $W.old -c core.hooksPath=/dev/null checkout -q --detach $old
  git -C $W.new -c core.hooksPath=/dev/null checkout -q --detach $new
  rm -rf $W.new/tests-coverage $W.new/coverage.xml
  of=(); for f in "${OLD[@]}"; do [ -e $W.old/$f ] && of+=($f); done
  nf=(); for f in "${NEW[@]}"; do [ -e $W.new/$f ] && nf+=($f); done
  # Python files this layer changes (still present), for the per-file style checks.
  mapfile -t changed < <(git -C $REPO diff --name-only --diff-filter=d $PARENT $new -- '*.py')
  lib_changed=(); for f in "${changed[@]}"; do case $f in lib/spack/spack/vendor/*) ;; lib/*) lib_changed+=($f);; esac; done
  out="L$n $(git -C $REPO log -1 --format=%h $new)"; detail=""; bad=0

  if want ids; then
    ids $W.old "${of[@]}" > /tmp/tm-old.ids; ids $W.new "${nf[@]}" > /tmp/tm-new.ids
    d=$(diff /tmp/tm-old.ids /tmp/tm-new.ids | grep -c '^[<>]')
    out="$out ids=$(wc -l </tmp/tm-new.ids) iddiff=$d"; [ "$d" = 0 ] || bad=1
  fi

  if want ruff; then
    lf=("${changed[@]}" "${LINT[@]}"); lfx=(); for f in "${lf[@]}"; do [ -e $W.new/$f ] && lfx+=($f); done
    if [ ${#lfx[@]} -gt 0 ]; then
      r=$(cd $W.new && { $VENV/ruff check -q "${lfx[@]}"; $VENV/ruff format --check -q "${lfx[@]}"; } 2>&1 | tail -3)
    else r=""; fi
    out="$out ruff=[${r}]"; [ -z "$r" ] || bad=1
  fi

  if want style; then
    st=$(cd $W.new && clean_env PATH="$VENV:$PATH" SPACK_PYTHON=$PY ./bin/spack style --base $PARENT 2>&1)
    ss=$?
    out="$out style=[$([ $ss = 0 ] && echo ok || echo FAIL)]"
    [ $ss = 0 ] || { bad=1; detail="$detail"$'\n'"    style: $(echo "$st" | grep -v -E '^(==>|Success|  (selected|import|ruff|mypy))' | head -6)"; }
  fi

  if want vermin && [ ${#lib_changed[@]} -gt 0 ]; then
    v=$(cd $W.new && clean_env PYTHONPATH=$W.new/lib/spack $VENV/vermin --backport importlib --backport argparse \
        --backport typing --violations -t=3.6- --no-tips --exclude-regex lib/spack/spack/vendor "${lib_changed[@]}" 2>&1)
    vs=$?
    # Embedded child-process code strings are invisible to vermin: scan added lines for 3.7+ keywords.
    kw=$(git -C $REPO diff -U0 $PARENT $new -- "${lib_changed[@]}" | grep -E '^\+.*\b(text|capture_output)=True' | head -3)
    out="$out vermin=[$([ $vs = 0 ] && [ -z "$kw" ] && echo ok || echo FAIL)]"
    [ $vs = 0 ] && [ -z "$kw" ] || { bad=1; detail="$detail"$'\n'"    vermin: $(echo "$v" | tail -5)$kw"; }
  fi

  if want pylint && [ ${#lib_changed[@]} -gt 0 ]; then
    p=$(cd $W.new && clean_env PYTHONPATH=$W.new/lib/spack $VENV/pylint -j 8 --disable=all --enable=unspecified-encoding \
        --ignore-paths=lib/spack/spack/vendor "${lib_changed[@]}" 2>&1 | grep -v -E '^(\*+ Module|$|-+$|Your code has been rated)')
    out="$out pylint=[$([ -z "$p" ] && echo ok || echo FAIL)]"; [ -z "$p" ] || { bad=1; detail="$detail"$'\n'"    pylint: $(echo "$p" | head -5)"; }
  fi

  if want mypy && [ ${#lib_changed[@]} -gt 0 ]; then
    m=$(cd $W.new && clean_env PYTHONPATH=$W.new/lib/spack $VENV/mypy --cache-dir=/tmp/tm-mypy-cache "${lib_changed[@]}" 2>&1 | tail -4)
    case $m in *"Success: no issues found"*) out="$out mypy=[ok]";; *) out="$out mypy=[FAIL]"; bad=1; detail="$detail"$'\n'"    mypy: $m";; esac
  fi

  if want license; then
    l=$(cd $W.new && clean_env PATH="$VENV:$PATH" PYTHONPATH=$W.new/lib/spack SPACK_PYTHON=$PY ./bin/spack license verify 2>&1 | tail -3)
    case $l in *"No license issues found"*) out="$out license=[ok]";; *) out="$out license=[FAIL]"; bad=1; detail="$detail"$'\n'"    license: $l";; esac
  fi

  if want slots; then
    s=$(cd $W.new && clean_env PATH="$VENV:$PATH" SPACK_PYTHON=$PY ./bin/spack python -m slotscheck \
        --exclude-modules="spack.test|spack.vendor|spack.installer.windows|spack.util.win_acl" lib/spack/spack/ 2>&1 | tail -4)
    case $s in *"All OK"*) out="$out slots=[ok]";; *) out="$out slots=[FAIL]"; bad=1; detail="$detail"$'\n'"    slots: $s";; esac
  fi

  if want cov; then
    # Test files the series changed up to this layer, plus COV_EXTRA.
    mapfile -t ct < <(git -C $REPO diff --name-only --diff-filter=d $BASE $new -- 'lib/spack/spack/test/*.py')
    covt=(); for f in "${ct[@]}" $COV_EXTRA; do case $f in */conftest.py|*/__init__.py) continue;; esac; [ -e $W.new/$f ] && covt+=($f); done
    (cd $W.new && clean_env PYTHONPATH=$W.new/lib/spack COVERAGE_FILE=$W.new/tests-coverage/.coverage $PY -m pytest \
        -p no:cacheprovider -q -n 8 --cov --cov-config=pyproject.toml --cov-report= "${covt[@]}" >/tmp/tm-cov.log 2>&1)
    (cd $W.new && $VENV/coverage xml --rcfile=pyproject.toml -o coverage.xml >/dev/null 2>&1)
    if [ -s $W.new/coverage.xml ]; then
      (cd $W.new && $VENV/diff-cover coverage.xml --compare-branch=$PARENT --json-report /tmp/tm-dc.json >/dev/null 2>&1)
      c=$($PY $HERE/cov_gate.py /tmp/tm-dc.json $MIN_COVERAGE $MIN_TOTAL "$NS_REGEX" $NS_TARGET); cs=$?
      out="$out cov=[$(echo "$c" | head -1)]"
      [ $cs = 0 ] || bad=1
      [ "$(echo "$c" | wc -l)" -gt 1 ] && detail="$detail"$'\n'"$(echo "$c" | tail -n +2)"
    else
      out="$out cov=[no data]"; bad=1; detail="$detail"$'\n'"    cov: $(tail -3 /tmp/tm-cov.log)"
    fi
  fi

  if want tests; then
    res=$(cd $W.new && clean_env PYTHONPATH=$W.new/lib/spack $PY -m pytest -p no:cacheprovider -q -n 8 "${nf[@]}" 2>&1 | tail -1)
    out="$out tests=[$res]"; case $res in *failed*|*error*) bad=1;; esac
  fi

  echo "$out$detail"; [ $bad = 0 ] || fail=1
done < "$LAYERS"
rm -f "$LAYERS"
git -C $REPO worktree remove --force $W.old; git -C $REPO worktree remove --force $W.new
exit $fail
