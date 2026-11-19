# Initial Plan: Robot Arm Trajectory Simulation

## Project Team

- Alex Chen
- Priya Natarajan
- Sam Okafor

## IP Definition

### What the IP does

Model predictive control (MPC) for robots chooses its next action by simulating
many candidate torque sequences forward in time and picking the best one. Each
control step therefore needs hundreds of short simulations, in real time, which
a lightweight embedded processor cannot deliver.

This IP simulates a batch of candidate trajectories for a planar two-link robot
arm in parallel, and reports for each one the arm's path and whether it hit an
obstacle. The project is inspired by **robomorphic computing**:

> S. M. Neuman, B. Plancher, T. Bourgeat, T. Tambe, S. Devadas and V. J. Reddi,
> "Robomorphic computing: a design methodology for domain-specific accelerators
> parameterized by robot morphology," ASPLOS 2021.

We do a much simpler version: two joints, a fixed time step and circular
obstacles in the plane.

### Mathematical operations

The state of the arm at step `k` is the joint angles `q[k] = (q1, q2)` and the
joint velocities `qd[k]`. Given joint torques `u[k]`, one semi-implicit Euler
step is:

```python
def step(q, qd, u, theta, dt):
    M = mass_matrix(q, theta)          # 2x2, depends on cos(q2)
    c = coriolis(q, qd, theta)         # 2-vector, depends on sin(q2)
    g = gravity(q, theta)              # 2-vector, depends on cos(q1), cos(q1 + q2)
    qdd = solve_2x2(M, u - c - g - theta.b * qd)   # closed-form 2x2 inverse
    qd = qd + dt * qdd
    q = q + dt * qd
    return q, qd
```

`theta` holds the link lengths, masses, inertias and joint friction `b`. The
closed-form expressions for `M`, `c` and `g` for a two-link arm are standard and
will be in our Python reference model.

After each step we check each link, a line segment from joint to joint, against
every obstacle, a circle with center `o` and radius `r`:

```python
def segment_hits_circle(p0, p1, o, r):
    d = p1 - p0
    t = clip(dot(o - p0, d) / dot(d, d), 0, 1)   # closest point on the segment
    return norm(p0 + t * d - o) <= r
```

A trajectory stops at the first step where any link hits any obstacle.

### Why it suits hardware acceleration

- **Parallel across trajectories.** The candidate trajectories are independent,
  so several dynamics units can run side by side.
- **Parallel across obstacles.** The collision check for one step is the same
  small computation repeated for every obstacle and both links.
- **Latency critical.** MPC must finish its simulations within one control
  period (a few milliseconds), so throughput on the processor is not enough.
- **Small, fixed-size arithmetic.** Each step is a fixed number of multiplies,
  one 2 × 2 solve and a few sines and cosines, with no data-dependent memory
  access.

### How it interacts with the PS

The PS sends the robot parameters `theta`, the obstacle list, the initial state
and a batch of torque sequences. The IP returns, for each trajectory, the joint
angles at every step and the step at which it collided, if it did.

## IP Architecture

### Sub-modules

1. **Command decoder.** Reads the configuration message (robot parameters and
   obstacles) into local registers and BRAM, then reads the torque sequences
   and hands each trajectory to a free dynamics unit.
2. **Dynamics unit** (four copies). Runs `step` for one trajectory per copy.
   Sine and cosine come from a lookup table with linear interpolation.
3. **Collision checker** (one per dynamics unit). Given the new joint
   positions, checks both links against all obstacles, with the obstacle loop
   unrolled by four.
4. **Output writer.** Collects the states from all dynamics units and writes
   them out in trajectory order, then sends the per-trajectory result.

### Interfaces

| From → To | Interface | What it carries |
|---|---|---|
| PS → Command decoder | AXI4-Stream input | a config message `{theta, n_obstacles, obstacles[]}`, then one message per trajectory `{traj_id, q0, qd0, nsteps, u[0..nsteps-1]}` |
| Command decoder → Dynamics unit | `hls::stream` | `{traj_id, q0, qd0, nsteps}` then one `u[k]` per step |
| Dynamics unit → Collision checker | `hls::stream` | joint positions `{p_elbow, p_tip}` for each step |
| Collision checker → Dynamics unit | `hls::stream` | one `hit` bit per step; the dynamics unit stops on a hit |
| Dynamics unit → Output writer | `hls::stream` | `{traj_id, k, q[k]}` per step |
| Output writer → PS | AXI4-Stream output | per trajectory: `{traj_id, nsteps_run, collided}` followed by `q[0..nsteps_run]` |

Values are 32-bit fixed point (Q16.16). AXI-Lite carries only start, done and
an error status.

### Why this decomposition

The dynamics and the collision check are different kinds of computation (a
serial chain of dependent steps, against a wide parallel comparison), so
separating them lets each be pipelined in its own way and tested on its own
against the Python model. Replicating the dynamics unit is how we get
parallelism across trajectories without changing its design.

### Existing IP

AMD's CORDIC IP can compute sine and cosine and could replace our lookup table;
we will compare the two on accuracy and resources.
