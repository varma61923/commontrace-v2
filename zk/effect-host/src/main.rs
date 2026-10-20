//! Actual RISC Zero receipt verification. Development receipts are always refused.
use hex::FromHex;
use risc0_zkvm::{sha::Digest, InnerReceipt, Receipt};
use serde::Deserialize;
use serde_json::json;
use std::io::{self, Read};
use unimem_effect_core::Statement;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Verification {
    receipt: Receipt,
    image_id: String,
    expected_challenge: String,
    expected_statement: Statement,
}
fn verify_receipt(receipt: &Receipt, image: Digest) -> Result<Statement, String> {
    if matches!(&receipt.inner, InnerReceipt::Fake(_)) {
        return Err("development receipts are not accepted".into());
    }
    if !matches!(
        &receipt.inner,
        InnerReceipt::Succinct(_) | InnerReceipt::Groth16(_)
    ) {
        return Err("private effect profile requires a succinct or Groth16 receipt".into());
    }
    receipt
        .verify(image)
        .map_err(|_| "RISC Zero cryptographic receipt verification failed")?;
    receipt
        .journal
        .decode()
        .map_err(|_| "invalid typed statement journal".into())
}
fn main() -> Result<(), Box<dyn std::error::Error>> {
    let action = std::env::args()
        .nth(1)
        .ok_or("expected describe, prove or verify")?;
    if action == "describe" {
        #[cfg(feature = "prove")]
        let image = Some(Digest::from(unimem_effect_methods::UNIMEM_EFFECT_GUEST_ID).to_string());
        #[cfg(not(feature = "prove"))]
        let image: Option<String> = None;
        println!(
            "{}",
            json!({"schema":"unimem.risc0-backend.v1", "sdk":"3.0.6", "image_id":image,
                             "proving":cfg!(feature="prove"), "development_receipts":false})
        );
        return Ok(());
    }
    let mut input = Vec::new();
    io::stdin()
        .take(16 * 1024 * 1024 + 1)
        .read_to_end(&mut input)?;
    if input.len() > 16 * 1024 * 1024 {
        return Err("proof request exceeds 16 MiB".into());
    }
    match action.as_str() {
        "verify" => {
            let request: Verification = serde_json::from_slice(&input)?;
            let image = Digest::from_hex(&request.image_id)?;
            let statement = verify_receipt(&request.receipt, image).map_err(io::Error::other)?;
            if statement != request.expected_statement
                || statement.challenge_hash != request.expected_challenge
            {
                return Err(
                    "receipt statement does not match the externally pinned challenge".into(),
                );
            }
            println!(
                "{}",
                json!({"verified":true,"statement":statement,"image_id":request.image_id})
            );
        }
        "prove" => {
            #[cfg(feature = "prove")]
            {
                use risc0_zkvm::{default_prover, ExecutorEnv, ProverOpts};
                use unimem_effect_core::Witness;
                // No remote proving service and no developer-mode receipts.
                std::env::set_var("RISC0_PROVER", "local");
                std::env::remove_var("RISC0_DEV_MODE");
                let witness: Witness = serde_json::from_slice(&input)?;
                let env = ExecutorEnv::builder().write(&witness)?.build()?;
                let result = default_prover().prove_with_opts(
                    env,
                    unimem_effect_methods::UNIMEM_EFFECT_GUEST_ELF,
                    &ProverOpts::succinct(),
                )?;
                let image = Digest::from(unimem_effect_methods::UNIMEM_EFFECT_GUEST_ID);
                let statement = verify_receipt(&result.receipt, image).map_err(io::Error::other)?;
                println!(
                    "{}",
                    json!({"backend":"risc0","receipt":result.receipt,
                    "statement":statement,"image_id":image.to_string(),"private_rows":true})
                );
            }
            #[cfg(not(feature = "prove"))]
            return Err(
                "RISC Zero proving toolchain is absent; rebuild with --features prove".into(),
            );
        }
        _ => return Err("unknown proof action".into()),
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn developer_receipt_is_refused_independently_of_environment_flags() {
        let image = Digest::from([0u32; 8]);
        let claim = risc0_zkvm::ReceiptClaim::ok(image, vec![]);
        let fake = risc0_zkvm::FakeReceipt::new(claim);
        let receipt = Receipt::new(InnerReceipt::Fake(fake), vec![]);
        assert!(verify_receipt(&receipt, image)
            .unwrap_err()
            .contains("development"));
    }
}
