import * as React from 'react';
import { View } from 'react-native';
import Svg, { Circle, G, Path, Polyline, Rect } from 'react-native-svg';

import type { CaptureMap } from '../../modules/scanner-capture';
import { C } from './theme';

type Props = {
  map?: CaptureMap;
  pose?: { x: number; z: number; yaw: number };
  size?: number;
};

/**
 * Top-down coverage map: ARKit mesh occupancy cells + walked trajectory + "you are here" cone.
 * ARKit is Y-up, so top-down uses (x, z); screen up = −z (the initial forward direction).
 */
export function Minimap({ map, pose, size = 150 }: Props) {
  const view = React.useMemo(() => {
    const xs: number[] = [];
    const zs: number[] = [];
    const add = (arr?: number[]) => {
      if (!arr) return;
      for (let i = 0; i + 1 < arr.length; i += 2) {
        xs.push(arr[i]);
        zs.push(arr[i + 1]);
      }
    };
    add(map?.coverage);
    add(map?.trajectory);
    if (pose) {
      xs.push(pose.x);
      zs.push(pose.z);
    }
    if (!xs.length) return null;
    const minX = Math.min(...xs) - 1;
    const maxX = Math.max(...xs) + 1;
    const minZ = Math.min(...zs) - 1;
    const maxZ = Math.max(...zs) + 1;
    const span = Math.max(4, maxX - minX, maxZ - minZ);
    const cx = (minX + maxX) / 2;
    const cz = (minZ + maxZ) / 2;
    const s = size / span;
    return { s, tx: (x: number) => (x - cx) * s + size / 2, tz: (z: number) => (z - cz) * s + size / 2 };
  }, [map, pose, size]);

  const cells: React.ReactNode[] = [];
  let traj = '';
  if (view && map) {
    const cs = Math.max(1, map.cell * view.s);
    for (let i = 0; i + 1 < map.coverage.length; i += 2) {
      cells.push(
        <Rect
          key={i}
          x={view.tx(map.coverage[i]) - cs / 2}
          y={view.tz(map.coverage[i + 1]) - cs / 2}
          width={cs}
          height={cs}
          fill={C.accent}
          opacity={0.35}
        />,
      );
    }
    const pts: string[] = [];
    for (let i = 0; i + 1 < map.trajectory.length; i += 2) {
      pts.push(`${view.tx(map.trajectory[i]).toFixed(1)},${view.tz(map.trajectory[i + 1]).toFixed(1)}`);
    }
    traj = pts.join(' ');
  }

  let cone: React.ReactNode = null;
  if (view && pose) {
    const px = view.tx(pose.x);
    const pz = view.tz(pose.z);
    // yaw = atan2(fwd.x, -fwd.z): 0 → screen up.
    const a = pose.yaw;
    const r = 18;
    const half = 0.5;
    const p1 = [px + r * Math.sin(a - half), pz - r * Math.cos(a - half)];
    const p2 = [px + r * Math.sin(a + half), pz - r * Math.cos(a + half)];
    cone = (
      <G>
        <Path d={`M${px},${pz} L${p1[0]},${p1[1]} L${p2[0]},${p2[1]} Z`} fill="#fff" opacity={0.35} />
        <Circle cx={px} cy={pz} r={4} fill="#fff" />
      </G>
    );
  }

  return (
    <View
      style={{
        width: size,
        height: size,
        borderRadius: 12,
        overflow: 'hidden',
        backgroundColor: 'rgba(0,0,0,0.55)',
        borderWidth: 1,
        borderColor: 'rgba(255,255,255,0.15)',
      }}
    >
      <Svg width={size} height={size}>
        {cells}
        {traj ? <Polyline points={traj} fill="none" stroke={C.warn} strokeWidth={2} /> : null}
        {cone}
      </Svg>
    </View>
  );
}
