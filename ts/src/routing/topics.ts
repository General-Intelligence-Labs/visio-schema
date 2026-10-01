/**
 * The Visio topic grammar: `/<root>/<sensor-group>/<index>/<sub-field>`.
 *
 * `docs/protocol/stream_type_map.md` §"Topic convention" defines this
 * normatively — the root is ALWAYS exactly one path segment, `<role>` for an
 * unhanded board and `<role>_<side>` once a limb is assigned (`<role>_<code8>`
 * while it is not). The root says where a board's data LANDS, so it is what
 * attributes a frame on a shared connection to the head or to a limb.
 *
 * This lives here because the spec does. It had been implemented only in
 * consumers, and partially: visio-companion carried the complete parser, while
 * this repo's own `display/scene_derivers.py` and `display/hand_geometry.py`
 * each carry a one-line `topic.strip("/").split("/")[0]` that ignores the
 * sensor-segment boundary entirely.
 *
 * This closes the TypeScript half only. There is still no
 * `visio_schema/routing/topics.py`, and those two one-liners are still there —
 * so the grammar remains without a canonical PYTHON implementation, and a root
 * ending in a digit is still parsed wrongly on that side.
 *
 * Pure string handling: no protobuf, no I/O.
 */

/**
 * The indexed SENSOR segments. Everything before the first of them is the
 * announcing board's root.
 *
 * Listed rather than inferred: a bare `<segment>/<digits>` rule would also cut
 * a board root that happens to end in a number, and the unindexed channels
 * (`/ego/system_health`, `/gripper_left/joint_states`) carry no boundary at
 * all. Keep in step with the stream table in stream_type_map.md.
 */
const SENSOR_SEGMENT_RE = /\/(?:camera|imu|encoder|audio|tactile)\/\d+/;

/**
 * The board a topic belongs to: `/gripper_left/camera/0` and
 * `/gripper_left/imu/0/raw` both -> `gripper_left`.
 *
 * A topic with no sensor segment keeps its whole path, so a caller labelling
 * one still gets something meaningful rather than an empty string.
 */
export function boardRootOfTopic(topic: string): string {
  return boardRootOrNull(topic) ?? topic.replace(/^\//, '');
}

/**
 * The board root, or null when the topic carries no sensor segment to cut at.
 *
 * The difference from {@link boardRootOfTopic} matters exactly once: when a
 * caller is SEARCHING for the root among a board's channels, a whole path
 * (`ego/command_result`) would be accepted as one, and every board on the
 * connection would then be named against it.
 */
export function boardRootOrNull(topic: string): string | null {
  const i = topic.match(SENSOR_SEGMENT_RE)?.index ?? -1;
  return i > 0 ? topic.slice(0, i).replace(/^\//, '') : null;
}

/**
 * Find a board root across a DeviceInfo channel list. Control channels often
 * precede sensors, so callers must SEARCH rather than inspect channels[0].
 */
export function boardRootOfTopics(topics: readonly string[]): string | null {
  for (const topic of topics) {
    const root = boardRootOrNull(topic);
    if (root) return root;
  }
  return null;
}

/**
 * The camera's own index, or null when the topic carries no `/camera/<n>` —
 * the caller decides what to fall back to, usually its position.
 */
export function cameraIndexOfTopic(topic: string): number | null {
  // `?.[1]` rather than `m[1]`: under `noUncheckedIndexedAccess` a capture
  // group is `string | undefined` even when the regex guarantees it.
  const digits = topic.match(/\/camera\/(\d+)/)?.[1];
  return digits === undefined ? null : parseInt(digits, 10);
}

/**
 * The part of a root after its role: `gripper_left` -> `left`,
 * `gripper_aB3xY9pQ` -> the code8, `ego` -> `ego` (no separator).
 *
 * The spec's guarantee that makes this unambiguous: a code8 contains no `-`
 * and no `_`, so a root splits on its FIRST underscore.
 */
export function rootSuffix(root: string): string {
  const i = root.indexOf('_');
  return i < 0 ? root : root.slice(i + 1);
}

/**
 * The side a board was calibrated to, read off the suffix of its root.
 *
 * Empty when the unit declares no side — an unassigned unit's code8 is not a
 * side, and a topic belonging to another role is not ours to read.
 *
 * Deliberately does NOT accept the old two-segment `/<role>/<side>/` form: a
 * leaf on firmware predating the single-segment root must read as unassigned
 * rather than be silently admitted. The firmware's own `hand_pair_check.hpp`
 * applies the same rule.
 */
export function sideOfTopics(
  role: string,
  topics: readonly string[],
): '' | 'left' | 'right' {
  const prefix = `/${role}_`;
  for (const t of topics) {
    if (!t.startsWith(prefix)) continue;
    const seg = t.slice(prefix.length).split('/', 1)[0];
    if (seg === 'left' || seg === 'right') return seg;
  }
  return '';
}
