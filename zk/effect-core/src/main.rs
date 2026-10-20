//! Native cross-language arithmetic checker; this is not a zkVM prover.
use std::io::{self, Read};
use unimem_effect_core::{evaluate, Witness};
fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut input = Vec::new();
    io::stdin()
        .take(16 * 1024 * 1024 + 1)
        .read_to_end(&mut input)?;
    if input.len() > 16 * 1024 * 1024 {
        return Err("witness exceeds 16 MiB".into());
    }
    let witness: Witness = serde_json::from_slice(&input)?;
    let statement = evaluate(&witness).map_err(io::Error::other)?;
    println!("{}", serde_json::to_string(&statement)?);
    Ok(())
}
