# Schema Epoch Reconcile

A distributed-state reconciliation challenge combining epoch-aware record selection, projective modular consensus, Chinese Remainder Theorem reconstruction, and bounded rational recovery.

The project models a system in which multiple workers report modular representations of the same underlying integer vector. Reports can arrive from different epochs, contain incomplete states, use different column orders, and represent the same mathematical line using different nonzero modular scale factors.

The objective is to reconcile those observations and recover one canonical primitive integer vector.

## Overview

Each worker produces observations modulo several primes.

A simplified view is:

```text
worker observations
        ↓
epoch/status reconciliation
        ↓
column-order restoration
        ↓
projective normalization
        ↓
strict-majority consensus
        ↓
CRT reconstruction
        ↓
bounded rational reconstruction
        ↓
primitive integer vector
```

The important challenge is that modular vectors cannot be compared coordinate-by-coordinate without accounting for their projective scale.

## Repository Structure

```text
schema-epoch-reconcile/
├── environment/
├── solution/
├── tests/
├── instruction.md
└── task.toml
```

### `instruction.md`

Defines the input records, reconciliation rules, projective equivalence, majority semantics, reconstruction bounds, and output schema.

### `environment/`

Contains the reproducible runtime and solver-visible data.

### `solution/`

Contains the reference implementation.

### `tests/`

Contains the independent verifier.

### `task.toml`

Defines task metadata and execution configuration.

## Epoch Reconciliation

Workers may produce several records for the same prime across multiple epochs.

Not every record should participate in consensus.

The authoritative observation is determined using the task's epoch and status rules.

A critical distinction is:

```text
latest record
```

versus:

```text
latest completed record
```

An incomplete or pending observation must not incorrectly replace an earlier completed result.

Conceptually:

```text
worker / prime
    │
    ├── epoch 4 → completed
    ├── epoch 5 → pending
    └── epoch 6 → absent

authoritative observation → epoch 4
```

The exact rules are defined in `instruction.md`.

## Column Restoration

Different workers may expose coordinates in different local orders.

Before mathematical comparison, each observation must be restored to the common global coordinate order.

Without this step, equivalent vectors can appear unrelated.

## Projective Equivalence

The modular observations represent projective vectors.

For a prime `p`, vectors

```text
v
```

and

```text
λv  (mod p)
```

represent the same projective point whenever `λ != 0 (mod p)`.

For example:

```text
[1, 2, 3] mod p
```

and

```text
[5, 10, 15] mod p
```

belong to the same projective class when multiplication by `5` is valid modulo `p`.

Therefore, raw coordinate equality is not the correct consensus criterion.

## Canonicalization

To compare observations, a projective representative can be normalized using a nonzero pivot coordinate.

Conceptually:

```text
v = [v0, v1, ..., vn]

choose first nonzero vpivot

normalize:

v / vpivot  (mod p)
```

Equivalent scaled observations then collapse to the same representative.

The implementation must also preserve the canonicalization and sign rules specified by the task contract.

## Strict-Majority Consensus

After authoritative observations are grouped by projective equivalence, a prime is accepted only if one class has a strict majority.

The acceptance condition is:

```text
support * 2 > votes
```

This deliberately differs from accepting a tie or simple plurality.

For example:

```text
votes = 6
support = 3
```

is not a strict majority.

But:

```text
votes = 7
support = 4
```

is.

The consensus report preserves the relevant vote/support information for each prime.

## Why Naive CRT Fails

A tempting approach is to normalize every modular vector independently and then apply the Chinese Remainder Theorem directly to each coordinate.

That can fail because each modular observation may have been scaled independently.

In other words:

```text
prime p1 → λ1 * x
prime p2 → λ2 * x
prime p3 → λ3 * x
```

where:

```text
λ1 != λ2 != λ3
```

Coordinate-wise CRT on those independently scaled values does not necessarily reconstruct the original integer vector.

The project therefore has to preserve projective relationships across the accepted primes.

## CRT Reconstruction

Once compatible projective information has been recovered, residues across accepted primes can be combined using the Chinese Remainder Theorem.

Conceptually:

```text
x ≡ a1 (mod p1)
x ≡ a2 (mod p2)
x ≡ a3 (mod p3)

             ↓

x mod (p1 * p2 * p3)
```

However, CRT alone provides a residue class, not necessarily the desired bounded rational relationship between coordinates.

## Rational Reconstruction

The task assumes a bounded primitive integer solution.

Ratio information recovered modulo the product of accepted primes can therefore be converted back into bounded rational values.

Conceptually:

```text
modular coordinate ratio
          ↓
CRT
          ↓
residue modulo M
          ↓
bounded rational reconstruction
          ↓
numerator / denominator
```

After recovering compatible coordinate ratios, denominators can be cleared to obtain an integer vector.

## Primitive Integer Vector

The reconstructed vector is reduced to primitive form.

For example:

```text
[12, 18, -6]
```

is not primitive because all coordinates share a common factor.

Dividing by the gcd gives:

```text
[2, 3, -1]
```

A canonical global sign is then applied according to the task rules.

## Reconstruction Conditions

The task supplies bounds that make the intended reconstruction uniquely identifiable.

The accepted-prime set must provide sufficient modulus for bounded reconstruction.

The authoritative numerical conditions are documented in `instruction.md`.

## Output

The result contains two main pieces:

```text
vector
consensus
```

`vector` is the recovered primitive integer vector.

`consensus` records the reconciliation result for the relevant primes, including fields such as:

```text
prime
state
support
votes
```

The exact schema, sort order, integer representation, and state values are defined by `instruction.md`. :chatgpt-content-reference{index="3"}

## Common Failure Modes

Several plausible implementations are mathematically incorrect.

### Choosing the latest epoch before filtering status

This can allow an unfinished record to hide the latest completed result.

### Using plurality instead of strict majority

The required condition is strict majority, not simply selecting the largest group.

### Comparing modular vectors literally

Different nonzero scale factors can represent the same projective vector.

### Running coordinate-wise CRT on scaled representatives

Independent projective scaling destroys the assumption required by naive coordinate reconstruction.

### Ignoring global column order

Local worker permutations must be resolved before consensus.

## Technical Areas

This project exercises:

- distributed state reconciliation
- epoch/version handling
- modular arithmetic
- projective equivalence
- consensus algorithms
- Chinese Remainder Theorem
- rational reconstruction
- integer normalization
- number theory
- deterministic data processing
- fault-tolerant reasoning

## Validation

A correct implementation should work for more than the supplied example.

The underlying algorithm must correctly handle:

- newer incomplete records
- different worker column permutations
- independently scaled modular observations
- tied or insufficient consensus
- multiple accepted primes
- bounded rational reconstruction
- primitive-vector normalization

The reference task was verified across multiple private scenarios covering these semantics. :chatgpt-content-reference{index="4"}

## Goal

The goal of Schema Epoch Reconcile is to recover one mathematically consistent global result from distributed observations that are individually incomplete, reordered, and projectively ambiguous.

The project combines distributed-systems reconciliation with exact number-theoretic reconstruction.

## License

No license is currently specified.
