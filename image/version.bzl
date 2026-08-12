"""Render the version an image carries from the components build configuration carries.

A version derived from git state cannot be computed inside the graph, so `bin/tine` queries git
for what one is made of and writes those components to build configuration. How much of the commit
hash a snapshot keeps is decided here rather than there: the budget belongs to the image, whose
longest partition label has to stay inside GPT's limit, and two images of one project share a git
state without sharing a budget.

"Image versioning" in images.md explains the shapes and their ordering.
"""

load(
    ":image.bzl",
    "AUTO",
    "AUTO_PREFIX",
    "FILENAME_PATTERN",
    "GPT_LABEL_LIMIT",
    "check_name",
)

_SECTION = "tine"

def _components(declared: str | None) -> struct | None:
    base = read_root_config(_SECTION, "version-base")
    commit = read_root_config(_SECTION, "version-commit")

    # A declared base counts in the height and a derived one in the distance from its tag, so only
    # the one about to be read has to be there.
    count = read_root_config(_SECTION, "version-height" if declared != None else "version-count")
    if base == None or commit == None or count == None:
        return None

    # Only a dirty tree has seconds, so their absence is what makes a build a release or a snapshot.
    seconds = read_root_config(_SECTION, "version-seconds")
    return struct(
        base = base,
        count = int(count),
        commit = commit,
        seconds = int(seconds) if seconds != None else None,
    )

def _room(labels: list[str], image_id: str) -> struct:
    """What the tightest label carrying a version leaves for one, and which label that is.

    Rendering the label twice is what counts its placeholders: `format` is the only thing that reads
    the escapes and repeats the way the rule that renders these labels for real will.
    """
    room = struct(characters = GPT_LABEL_LIMIT, label = None)
    for label in labels:
        empty = len(label.format(image_id = image_id, version = ""))
        occurrences = len(label.format(image_id = image_id, version = "x")) - empty
        if occurrences == 0:
            continue
        characters = (GPT_LABEL_LIMIT - empty) // occurrences
        if characters < room.characters:
            room = struct(characters = characters, label = label)
    return room

def _render(what: str, components: struct, declared: str | None, image_id: str, room: struct) -> str:
    base = declared if declared != None else components.base
    count = components.count

    if components.seconds != None:
        # Seconds only grow, and nothing here can shorten them, so this is the one shape whose fit
        # can lapse with time rather than with what the checkout says.
        version = "{}^{}^{}".format(base, count, components.seconds)
    elif declared == None and count == 0:
        # Only the version a tag names can be spelled bare. A declared base names no commit, so it
        # always carries what tells two builds of it apart.
        version = base
    else:
        # git only lengthens the abbreviation on ambiguity, so shrink manually: the hash names the
        # commit for tracking, ordering comes from the base and the commit count. Never below four
        # characters, so a budget that cannot hold one leaves the shortest candidate to be reported.
        version = "{}^{}-{}".format(base, count, components.commit[:12])
        for length in range(11, 3, -1):
            if len(version) <= room.characters:
                break
            version = "{}^{}-{}".format(base, count, components.commit[:length])

    if len(version) > room.characters:
        fail(
            "{}: version {!r} needs {} characters where label {!r} leaves {} of GPT's {}".format(
                what,
                version,
                len(version),
                room.label,
                room.characters,
                GPT_LABEL_LIMIT,
            )
            + ' (image_id "{}" takes {}). Shorten the image_id, or declare a shorter version.'.format(
                image_id,
                len(image_id),
            ),
        )
    return version

def resolve_version(what: str, version: str | Select | None, *, labels: list[str] = [], image_id: str = "") -> str | Select | None:
    """Resolve `"auto"` and `"auto:<base>"` against configuration, leaving every other version alone.

    `"auto"` takes its base from the latest tag as well; `"auto:1.4.2"` declares the base and takes
    only what orders one build of it against the next. `labels` are the partition label patterns the
    image carries, of which the longest decides how much of the commit hash survives; an image whose
    version reaches no label passes none.
    """
    if type(version) != type(AUTO):
        return version
    if version == AUTO:
        declared = None
    elif version.startswith(AUTO_PREFIX):
        declared = check_name(what + " version", version[len(AUTO_PREFIX) :], FILENAME_PATTERN)
    else:
        return version

    components = _components(declared)
    if components == None:
        fail(
            "{}: version {!r} needs the version components `bin/tine` writes to ".format(what, version)
            + ".buckconfig.local, which says there why it wrote none; build through `bin/tine`, or "
            + "name the version outright",
        )
    return _render(what, components, declared, image_id, _room(labels, image_id))
