"""Print every VIVE pose from an MCAP without inventing per-tracker topics.

Run with the source schema installed: python read_vive.py recording.mcap
The same iter_poses(message, channel) works on read_serial() or resolved bus rows.
"""
from __future__ import annotations

import argparse

from visio_schema import message_class, read_mcap


def iter_poses(message, channel):
    """Yield (tracker_serial, aligned_ns, frame_id, map_id, foxglove.Pose)."""
    if channel.schema_name != "visio_schema.v1.sensor.VivePoseBatch":
        return
    batch = message_class(channel.schema_name)()
    batch.ParseFromString(message.payload)
    anchor_ns = message.timestamp.ToNanoseconds()
    for tracker in batch.trackers:
        for sample in tracker.samples:
            yield (
                tracker.tracker_serial,
                anchor_ns + sample.t_offset_ns,
                batch.frame_id,
                batch.station_map_id,
                sample.pose,
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mcap")
    args = parser.parse_args()
    for message, channel in read_mcap(args.mcap):
        for serial, ns, frame, map_id, pose in iter_poses(message, channel):
            p, q = pose.position, pose.orientation
            print(serial, ns, frame, map_id, p.x, p.y, p.z, q.x, q.y, q.z, q.w)


if __name__ == "__main__":
    main()
