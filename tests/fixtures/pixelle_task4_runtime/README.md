# Pixelle Task 4 runtime fixture

`frame_processor_post_0010.zip` contains the exact pinned upstream
`pixelle_video/services/frame_processor.py` after patches `0001` through `0010`
and before patch `0011`.

The Task 4 runtime tests extract the real `frame_processor.py` section from
`0011-render-talking-material-scenes.patch`, apply it with the installer flag
`--unidiff-zero`, load the patched module with narrow dependency stubs, and
exercise fallback and temporary-file behavior. This keeps the tests offline
while proving behavior from the production patch rather than checking strings.
