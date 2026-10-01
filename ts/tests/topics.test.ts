/**
 * The topic grammar, against docs/protocol/stream_type_map.md §"Topic
 * convention" — the normative source, which until now had no canonical
 * implementation in any language.
 *
 * The cases carried over from visio-companion (whose parser this is) plus the
 * rules the spec states that the app's suite did not cover: the one-segment
 * guarantee, the code8-has-no-underscore guarantee, and the optional host
 * prefix readers are told to strip.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  boardRootOfTopic,
  boardRootOfTopics,
  boardRootOrNull,
  cameraIndexOfTopic,
  rootSuffix,
  sideOfTopics,
} from '../src/routing/topics.js';

test('the sensor segment and the leading slash are stripped', () => {
  assert.equal(boardRootOfTopic('/gripper_left/camera/0'), 'gripper_left');
  assert.equal(boardRootOfTopic('/gripper_left/imu/0/raw'), 'gripper_left');
});

test('every indexed sensor kind roots the same board', () => {
  for (const t of [
    '/ego/camera/0', '/ego/imu/0/raw', '/ego/encoder/1/raw',
    '/ego/audio/0', '/ego/tactile/0',
  ]) {
    assert.equal(boardRootOfTopic(t), 'ego', t);
  }
});

test('a root ending in a number is not mistaken for a sensor segment', () => {
  // The reason the kinds are listed rather than inferred from `<seg>/<digits>`.
  assert.equal(boardRootOfTopic('/ego_v2/camera/0'), 'ego_v2');
  assert.equal(boardRootOfTopic('/rig2/imu/0/raw'), 'rig2');
});

test('a topic with no indexed sensor segment keeps its whole path', () => {
  assert.equal(boardRootOfTopic('/ego/system_health'), 'ego/system_health');
  assert.equal(boardRootOfTopic('/gripper_left/joint_states'), 'gripper_left/joint_states');
});

test('boardRootOrNull refuses to invent a root', () => {
  // The distinction that matters: a caller SEARCHING for the root among
  // channels would otherwise accept 'ego/command_result' as one, and name
  // every board on the connection against it.
  assert.equal(boardRootOrNull('/ego/command_result'), null);
  assert.equal(boardRootOrNull('/ego/camera/0'), 'ego');
});

test('the search skips control channels to find the root', () => {
  assert.equal(
    boardRootOfTopics(['/ego/command_result', '/ego/system_health', '/ego/camera/0']),
    'ego',
  );
  assert.equal(boardRootOfTopics(['/ego/command_result']), null);
});

test('two limbs of one rig root differently — the spec forbids sharing', () => {
  // "a mirrored pair must NOT share it — two limbs publishing one root
  // collide, and the loser's streams are dropped."
  assert.notEqual(
    boardRootOfTopic('/gripper_left/camera/0'),
    boardRootOfTopic('/gripper_right/camera/0'),
  );
});

test('the camera index is read, or null when absent', () => {
  assert.equal(cameraIndexOfTopic('/ego/camera/3'), 3);
  assert.equal(cameraIndexOfTopic('/ego/camera/0'), 0);
  assert.equal(cameraIndexOfTopic('/ego/imu/0/raw'), null);
  assert.equal(cameraIndexOfTopic('/ego/system_health'), null);
});

test('a calibrated side is read off the root suffix', () => {
  assert.equal(sideOfTopics('gripper', ['/gripper_left/camera/0']), 'left');
  assert.equal(sideOfTopics('gripper', ['/gripper_right/imu/0/raw']), 'right');
});

test('an unassigned code8 root is not a side', () => {
  // "An unassigned limb roots at its code8 rather than guessing a side."
  assert.equal(sideOfTopics('gripper', ['/gripper_aB3xY9pQ/camera/0']), '');
});

test("another role's topics are not ours to read", () => {
  assert.equal(sideOfTopics('gripper', ['/glove_left/camera/0']), '');
});

test('the old two-segment /<role>/<side>/ form reads as unassigned', () => {
  // Deliberate: a leaf on firmware predating the single-segment root must not
  // be silently admitted. The firmware's hand_pair_check.hpp agrees.
  assert.equal(sideOfTopics('gripper', ['/gripper/left/camera/0']), '');
});

test('the first declared side wins over later unassigned topics', () => {
  assert.equal(
    sideOfTopics('gripper', ['/gripper_left/camera/0', '/gripper_aB3xY9pQ/imu/0/raw']),
    'left',
  );
});

test('rootSuffix splits on the FIRST underscore', () => {
  // The spec's guarantee that makes this unambiguous: a code8 contains no '-'
  // and no '_'.
  assert.equal(rootSuffix('gripper_left'), 'left');
  assert.equal(rootSuffix('gripper_aB3xY9pQ'), 'aB3xY9pQ');
  assert.equal(rootSuffix('ego'), 'ego');
});

test('the root is always exactly ONE segment', () => {
  // The spec states it as an invariant; this is what depends on it — a root
  // containing '/' would make rootSuffix and sideOfTopics both wrong.
  for (const t of ['/ego/camera/0', '/gripper_left/imu/0/raw', '/ego_v2/audio/0']) {
    assert.ok(!boardRootOfTopic(t).includes('/'), t);
  }
});
