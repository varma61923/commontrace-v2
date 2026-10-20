//! Shared integer evaluator for native checks and the actual RISC Zero guest.
//! Neither a valid statement nor a valid receipt establishes execution truth.
use num_bigint::BigUint;
use num_traits::One;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};

pub const SCALE: i128 = 1_000_000;
pub const MAX_ROWS: usize = 30_000;
const QUANTUM: i128 = 1_000;
const RANDOM_BITS: usize = 14_256;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Noise {
    pub numerator: u32,
    pub denominator: u32,
    pub noise_key: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Plan {
    pub schema: String,
    pub study: String,
    pub scale: u32,
    pub count: usize,
    pub cohort_root: String,
    pub propensity_min: u32,
    pub alpha_ppm: u32,
    pub kind: String,
    pub harm_limit_ppm: Option<u32>,
    pub commitment_key: String,
    pub dp: Option<Noise>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Challenge {
    pub plan_hash: String,
    pub nonce: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Row {
    pub episode: String,
    pub propensity: u32,
    pub assigned: bool,
    pub outcome: u32,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Witness {
    pub plan: Plan,
    pub challenge: Challenge,
    pub rows: Vec<Row>,
    pub commitment_seed: String,
    pub noise_seed: Option<String>,
}
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NoiseStatement {
    pub mechanism: String,
    pub epsilon_upper_ppm: i128,
    pub delta_upper_power_of_two: i32,
    pub quantum_ppm: i128,
    pub conditions: String,
}
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Statement {
    pub schema: String,
    pub plan_hash: String,
    pub challenge_hash: String,
    pub rows_root: String,
    pub count: usize,
    pub kind: String,
    pub scale: u32,
    pub alpha_ppm: u32,
    pub estimate_ppm: i128,
    pub hajek_ppm: Option<i128>,
    pub hajek_rounding_error_ppm: Option<u32>,
    pub lower_ppm: i128,
    pub upper_ppm: i128,
    pub rounding_error_ppm: u32,
    pub confidence: String,
    pub harm_limit_ppm: Option<u32>,
    pub harm_claim: Option<bool>,
    pub noise: Option<NoiseStatement>,
    pub execution_attested: bool,
}

fn require(ok: bool, message: &str) -> Result<(), String> {
    if ok {
        Ok(())
    } else {
        Err(message.to_owned())
    }
}
fn hash(bytes: &[u8]) -> [u8; 32] {
    Sha256::digest(bytes).into()
}
fn key(value: &str) -> Result<[u8; 32], String> {
    require(
        value.len() == 64
            && value
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)),
        "noncanonical hex commitment",
    )?;
    hex::decode(value)
        .map_err(|_| "invalid hex".to_owned())?
        .try_into()
        .map_err(|_| "invalid key length".to_owned())
}
fn identifier(value: &str) -> Result<Vec<u8>, String> {
    require(
        !value.is_empty()
            && value.len() <= 128
            && value
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"_.:/-".contains(&b)),
        "invalid episode or study identifier",
    )?;
    let mut out = (value.len() as u16).to_le_bytes().to_vec();
    out.extend_from_slice(value.as_bytes());
    Ok(out)
}
fn canonical(value: Value) -> Value {
    match value {
        Value::Object(map) => {
            let mut pairs: Vec<_> = map.into_iter().collect();
            pairs.sort_by(|a, b| a.0.cmp(&b.0));
            Value::Object(pairs.into_iter().map(|(k, v)| (k, canonical(v))).collect())
        }
        Value::Array(values) => Value::Array(values.into_iter().map(canonical).collect()),
        other => other,
    }
}
pub fn canonical_hash<T: Serialize>(value: &T) -> Result<String, String> {
    let json = serde_json::to_value(value).map_err(|_| "invalid JSON value")?;
    let bytes = serde_json::to_vec(&canonical(json)).map_err(|_| "invalid JSON encoding")?;
    Ok(hex::encode(hash(&bytes)))
}
fn tree(hashes: &[[u8; 32]]) -> [u8; 32] {
    if hashes.is_empty() {
        return hash(&[]);
    }
    if hashes.len() == 1 {
        return hashes[0];
    }
    let power = usize::BITS - 1 - (hashes.len() - 1).leading_zeros();
    let split = 1usize << power;
    let mut encoded = vec![1u8];
    encoded.extend_from_slice(&tree(&hashes[..split]));
    encoded.extend_from_slice(&tree(&hashes[split..]));
    hash(&encoded)
}
fn seed_key(seed: &[u8; 32], noise: bool) -> String {
    let mut encoded = if noise {
        b"unimem-effect-noise-v1\0".to_vec()
    } else {
        b"unimem-effect-hiding-v1\0".to_vec()
    };
    encoded.extend_from_slice(seed);
    hex::encode(hash(&encoded))
}
fn assigned(plan_hash: &[u8; 32], nonce: &[u8; 32], row: &Row) -> Result<bool, String> {
    let mut prefix = b"unimem-effect-assignment-v1\0".to_vec();
    prefix.extend_from_slice(nonce);
    prefix.extend_from_slice(plan_hash);
    prefix.extend_from_slice(&identifier(&row.episode)?);
    let modulus = BigUint::one() << 256usize;
    let scale = BigUint::from(SCALE as u32);
    let limit = &modulus / &scale * &scale;
    for counter in 0u32..16 {
        let mut encoded = prefix.clone();
        encoded.extend_from_slice(&counter.to_le_bytes());
        let value = BigUint::from_bytes_be(&hash(&encoded));
        if value < limit {
            return Ok(value % &scale < BigUint::from(row.propensity));
        }
    }
    Err("assignment rejection bound reached".into())
}
fn logs(alpha: u32, dp: bool) -> Result<i128, String> {
    let pair = match alpha {
        50_000 => (3_688_880, 4_382_027),
        10_000 => (5_298_318, 5_991_465),
        1_000 => (7_600_903, 8_294_050),
        1 => (14_508_658, 15_201_805),
        _ => return Err("unsupported alpha".into()),
    };
    Ok(if dp { pair.1 } else { pair.0 })
}
fn ceil(n: i128, d: i128) -> i128 {
    (n + d - 1) / d
}
fn sqrt_ceil(value: i128) -> i128 {
    if value == 0 {
        return 0;
    }
    let value = value as u128;
    let mut root = 1u128 << ((128 - value.leading_zeros()).div_ceil(2));
    loop {
        let next = (root + value / root) / 2;
        if next >= root {
            break;
        }
        root = next;
    }
    (root + u128::from(root * root != value)) as i128
}
fn radius(plan: &Plan, dp: bool) -> Result<i128, String> {
    let p = i128::from(plan.propensity_min);
    let width = if plan.kind == "harm" {
        SCALE
    } else {
        ceil(SCALE * SCALE, p) + ceil(SCALE * SCALE, SCALE - p)
    };
    let squared = width.checked_mul(width).ok_or("128-bit bound")?;
    let numerator = squared
        .checked_mul(logs(plan.alpha_ppm, dp)?)
        .ok_or("128-bit bound")?;
    Ok(sqrt_ceil(ceil(numerator, 2 * plan.count as i128 * SCALE)))
}
fn noise_parameters(plan: &Plan) -> Result<(u32, u32, i128), String> {
    let dp = plan.dp.as_ref().ok_or("missing noise profile")?;
    let log = match (dp.numerator, dp.denominator) {
        (3, 5) => 510_826,
        (9, 10) => 105_361,
        (99, 100) => 10_051,
        _ => return Err("unsupported finite noise fraction".into()),
    };
    key(&dp.noise_key)?;
    let sensitivity = if plan.kind == "harm" {
        ceil(SCALE, plan.count as i128) + 1
    } else {
        ceil(
            SCALE * SCALE,
            plan.count as i128 * i128::from(plan.propensity_min),
        ) + 2
    };
    let maximum = if plan.kind == "harm" {
        SCALE / QUANTUM
    } else {
        2 * SCALE / QUANTUM
    };
    let epsilon = 2 * maximum.min(ceil(sensitivity, QUANTUM)) * log;
    require(epsilon <= 20 * SCALE, "noise profile exceeds epsilon bound")?;
    Ok((dp.numerator, dp.denominator, epsilon))
}
fn noisy(plan: &Plan, estimate: i128, seed: &[u8; 32]) -> Result<(i128, i128, i128), String> {
    let (n, d, epsilon) = noise_parameters(plan)?;
    let low = if plan.kind == "harm" { 0 } else { -SCALE };
    let maximum = ((SCALE - low) / QUANTUM) as u32;
    let center = ((estimate.clamp(low, SCALE) - low) / QUANTUM) as u32;
    let base_n = BigUint::from(n);
    let base_d = BigUint::from(d);
    let weights: Vec<_> = (0..=maximum)
        .map(|index| {
            let distance = index.abs_diff(center);
            base_n.pow(distance) * base_d.pow(maximum - distance)
        })
        .collect();
    let total: BigUint = weights.iter().cloned().sum();
    require(total.bits() <= 14_000, "noise weights exceed 14000 bits")?;
    let mut prefix = b"unimem-effect-dp-draw-v1\0".to_vec();
    prefix.extend_from_slice(seed);
    prefix.extend_from_slice(&key(&canonical_hash(plan)?)?);
    let mut random = Vec::new();
    for block in 0..RANDOM_BITS.div_ceil(256) {
        let mut encoded = prefix.clone();
        encoded.extend_from_slice(&(block as u32).to_le_bytes());
        random.extend_from_slice(&hash(&encoded));
    }
    let mask = (BigUint::one() << RANDOM_BITS) - BigUint::one();
    let mut draw = (BigUint::from_bytes_be(&random) & mask) % total;
    let mut selected = 0;
    for (index, weight) in weights.iter().enumerate() {
        if draw < *weight {
            selected = index;
            break;
        }
        draw -= weight;
    }
    let mut tail = maximum;
    for distance in 0..maximum {
        let power = distance + 1;
        let left = BigUint::from(8 * SCALE as u32) * base_n.pow(power) * &base_d;
        let right = BigUint::from(plan.alpha_ppm) * BigUint::from(d - n) * base_d.pow(power);
        if left <= right {
            tail = distance;
            break;
        }
    }
    Ok((
        low + selected as i128 * QUANTUM,
        i128::from(tail) * QUANTUM + QUANTUM,
        epsilon,
    ))
}

pub fn evaluate(witness: &Witness) -> Result<Statement, String> {
    let plan = &witness.plan;
    require(
        plan.schema == "unimem.effect-plan.v1" && plan.scale == SCALE as u32,
        "unknown proof schema",
    )?;
    require(
        (2..=MAX_ROWS).contains(&witness.rows.len()) && witness.rows.len() == plan.count,
        "cohort count bound",
    )?;
    identifier(&plan.study)?;
    require(
        (50_000..=500_000).contains(&plan.propensity_min),
        "invalid minimum propensity",
    )?;
    logs(plan.alpha_ppm, false)?;
    require(
        plan.kind == "effect" || plan.kind == "harm",
        "unknown statement kind",
    )?;
    require(
        if plan.kind == "harm" {
            plan.harm_limit_ppm.is_some_and(|v| v <= SCALE as u32)
        } else {
            plan.harm_limit_ppm.is_none()
        },
        "harm threshold must be precommitted",
    )?;
    let plan_hash = canonical_hash(plan)?;
    require(
        witness.challenge.plan_hash == plan_hash,
        "challenge for another plan",
    )?;
    let nonce = key(&witness.challenge.nonce)?;
    let hiding = key(&witness.commitment_seed)?;
    require(
        seed_key(&hiding, false) == plan.commitment_key,
        "uncommitted outcome hiding seed",
    )?;
    let noise_seed = match (&plan.dp, &witness.noise_seed) {
        (None, None) => None,
        (Some(dp), Some(seed)) => {
            let seed = key(seed)?;
            require(
                seed_key(&seed, true) == dp.noise_key,
                "uncommitted noise seed",
            )?;
            noise_parameters(plan)?;
            Some(seed)
        }
        _ => return Err("unexpected or missing noise seed".into()),
    };
    let mut rows: Vec<_> = witness.rows.iter().collect();
    rows.sort_by(|a, b| a.episode.cmp(&b.episode));
    let mut cohort_leaves = Vec::new();
    let mut row_leaves = Vec::new();
    let mut minimum = SCALE as u32;
    let (mut treated, mut control, mut wt, mut wc, mut total) = (0i128, 0i128, 0i128, 0i128, 0i128);
    let plan_digest = key(&plan_hash)?;
    for (index, row) in rows.iter().enumerate() {
        let mut meta = identifier(&row.episode)?;
        require(
            index == 0 || rows[index - 1].episode != row.episode,
            "duplicate episode",
        )?;
        require(
            (50_000..=950_000).contains(&row.propensity) && row.outcome <= SCALE as u32,
            "row fixed-point bounds",
        )?;
        minimum = minimum.min(row.propensity.min(SCALE as u32 - row.propensity));
        meta.extend_from_slice(&row.propensity.to_le_bytes());
        let mut cohort_leaf = vec![2u8];
        cohort_leaf.extend_from_slice(&meta);
        cohort_leaves.push(hash(&cohort_leaf));
        require(
            assigned(&plan_digest, &nonce, row)? == row.assigned,
            "assignment differs from verifier nonce",
        )?;
        let mut leaf = vec![0u8];
        leaf.extend_from_slice(&hiding);
        leaf.extend_from_slice(&(index as u32).to_le_bytes());
        leaf.extend_from_slice(&identifier(&row.episode)?);
        leaf.push(u8::from(row.assigned));
        leaf.extend_from_slice(&row.propensity.to_le_bytes());
        leaf.extend_from_slice(&row.outcome.to_le_bytes());
        row_leaves.push(hash(&leaf));
        let (p, y) = (i128::from(row.propensity), i128::from(row.outcome));
        let score = if plan.kind == "harm" {
            y
        } else if row.assigned {
            let value = y * SCALE / p;
            treated += value;
            wt += SCALE * SCALE / p;
            value
        } else {
            let value = y * SCALE / (SCALE - p);
            control += value;
            wc += SCALE * SCALE / (SCALE - p);
            -value
        };
        total = total.checked_add(score).ok_or("128-bit sum bound")?;
    }
    require(
        hex::encode(tree(&cohort_leaves)) == plan.cohort_root && minimum == plan.propensity_min,
        "rows differ from complete precommitted cohort",
    )?;
    let mut estimate = total.div_euclid(plan.count as i128);
    let mut hajek = if wt != 0 && wc != 0 {
        Some(SCALE * treated / wt - SCALE * control / wc)
    } else {
        None
    };
    let rounding = if plan.kind == "harm" { 1 } else { 2 };
    let mut spread = radius(plan, false)?;
    let noise = if let Some(seed) = noise_seed {
        let (released, noise_spread, epsilon) = noisy(plan, estimate, &seed)?;
        estimate = released;
        spread = radius(plan, true)? + noise_spread;
        hajek = None;
        Some(NoiseStatement {
            mechanism: "finite-grid exponential".into(),
            epsilon_upper_ppm: epsilon,
            delta_upper_power_of_two: -224,
            quantum_ppm: QUANTUM,
            conditions:
                "secret independent seeds, cryptographic hiding/PRG assumptions and one release"
                    .into(),
        })
    } else {
        None
    };
    let low = if plan.kind == "harm" { 0 } else { -SCALE };
    let lower = (estimate - spread - rounding).clamp(low, SCALE);
    let upper = (estimate + spread + rounding).clamp(low, SCALE);
    Ok(Statement {
        schema: "unimem.effect-statement.v1".into(),
        plan_hash,
        challenge_hash: canonical_hash(&witness.challenge)?,
        rows_root: hex::encode(tree(&row_leaves)),
        count: rows.len(),
        kind: plan.kind.clone(),
        scale: SCALE as u32,
        alpha_ppm: plan.alpha_ppm,
        estimate_ppm: estimate,
        hajek_ppm: hajek,
        hajek_rounding_error_ppm: hajek.map(|_| 6),
        lower_ppm: lower,
        upper_ppm: upper,
        rounding_error_ppm: rounding as u32,
        confidence: "bounded fixed-cohort Hoeffding; assignment and sampling assumptions required"
            .into(),
        harm_limit_ppm: plan.harm_limit_ppm,
        harm_claim: plan.harm_limit_ppm.map(|limit| upper <= i128::from(limit)),
        noise,
        execution_attested: false,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn integer_roots_are_ceiled() {
        for n in 0..10_000 {
            let r = sqrt_ceil(n);
            assert!(r * r >= n && (r == 0 || (r - 1) * (r - 1) < n));
        }
    }
    #[test]
    fn invalid_private_input_never_produces_a_statement() {
        let raw = r#"{"plan":{},"challenge":{},"rows":[],"commitment_seed":"","noise_seed":null}"#;
        assert!(serde_json::from_str::<Witness>(raw).is_err());
    }
}
