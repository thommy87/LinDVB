#!/bin/sh
# Extract the translatable strings into lindvb/po/lindvb.pot and merge them
# into every lindvb/po/<lang>.po (needs gettext: xgettext, msgmerge).
set -e
cd "$(dirname "$0")/.."
xgettext --language=Python --from-code=UTF-8 --keyword=_ --keyword=N_ \
    --add-comments=TRANSLATORS --no-wrap --sort-by-file --package-name=LinDVB \
    -o lindvb/po/lindvb.pot $(find lindvb -name '*.py' | sort)
for po in lindvb/po/*.po; do
    [ -e "$po" ] || continue
    msgmerge --quiet --update --no-wrap --backup=none "$po" lindvb/po/lindvb.pot
done
