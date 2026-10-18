#![no_main]
risc0_zkvm::guest::entry!(main);
fn main() {
    let witness: unimem_effect_core::Witness = risc0_zkvm::guest::env::read();
    let statement =
        unimem_effect_core::evaluate(&witness).expect("invalid committed effect witness");
    risc0_zkvm::guest::env::commit(&statement);
}
