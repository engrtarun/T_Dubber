//! Assembles `cpp_accelerator/kernels/stitcher_mix.asm` and links it into the
//! `stitcher` binary.
//!
//! WHY A BUILD SCRIPT FOR ONE .ASM FILE
//! ------------------------------------
//! The kernels are written once in NASM and assembled twice -- `-f elf64` for the
//! Linux pack, `-f win64` for local Windows builds -- exactly like the C++
//! normalizer's kernel. What differs here is that nothing needs compiling
//! around it: `stitcher_mix.asm` exports `td_stitcher_lay`, `td_stitcher_duck`
//! and `td_stitcher_add_voice` with C linkage directly, and the first argument
//! register is RCX in both the SysV and the Microsoft ABI, so the same object
//! links into either. That means no C shim, no `cc` crate, no C++ compiler in
//! this stage -- only nasm and a link argument.
//!
//! WHAT THIS IS WORTH
//! ------------------
//! The loop the `lay` kernel replaces ran one 64-bit integer division per
//! sample, because it indexed the background with `i % bg.len()`. At the
//! stitcher's 24 kHz that is 2.57M divisions for a 107 s trailer and 172.8M
//! for a two-hour film, and a 64-bit idiv neither pipelines nor overlaps with
//! the multiply next to it. The kernel copies `min(bg left, timeline left)` per
//! run and resets the read pointer, so no division is executed at all.
//!
//! FAILURE IS A WARNING, NOT AN ERROR -- AND WHY THAT IS SAFE HERE
//! ---------------------------------------------------------------
//! If nasm is missing or the source does not assemble, the build prints a
//! cargo warning and compiles the scalar path instead. Unlike the normalizer --
//! whose Python fallback silently changed the audio and then failed a quality
//! gate 20 minutes later -- this fallback is the same Rust arithmetic, produces
//! bit-identical samples, and no longer contains the division (see
//! `lay_background` in main.rs). So the degraded build is a slower build, not a
//! different one, which is what makes a soft failure defensible.
//!
//! The pack build wants the opposite, because there the binary is the product:
//! set `TD_STITCHER_REQUIRE_ASM=1` and a missing kernel stops the build instead
//! of shipping a slower stitcher nobody notices.

use std::env;
use std::path::PathBuf;
use std::process::Command;

fn main() {
    println!("cargo:rerun-if-changed=../cpp_accelerator/kernels/stitcher_mix.asm");
    println!("cargo:rerun-if-changed=build.rs");
    println!("cargo:rerun-if-env-changed=TD_STITCHER_NO_ASM");
    println!("cargo:rerun-if-env-changed=TD_STITCHER_REQUIRE_ASM");
    println!("cargo:rerun-if-env-changed=NASM");
    // main.rs gates its extern block on this, and cargo warns about any cfg it
    // was not told about. Declaring it here keeps that warning away.
    println!("cargo:rustc-check-cfg=cfg(td_stitcher_asm)");

    if env::var_os("TD_STITCHER_NO_ASM").is_some() {
        println!("cargo:warning=TD_STITCHER_NO_ASM is set; building the scalar stitcher");
        return;
    }

    let manifest_dir = PathBuf::from(env::var("CARGO_MANIFEST_DIR").expect("CARGO_MANIFEST_DIR"));
    let asm = manifest_dir.join("../cpp_accelerator/kernels/stitcher_mix.asm");
    if !asm.exists() {
        fail_or_warn(format!(
            "mix kernel missing at {}; the scalar stitcher is bit-identical but slower",
            asm.display()
        ));
        return;
    }

    let target_os = env::var("CARGO_CFG_TARGET_OS").unwrap_or_default();
    // Both ABIs pass the struct pointer in RCX, but the OBJECT format differs:
    // COFF for MSVC targets, ELF for everything else.
    let format = match target_os.as_str() {
        "windows" => "win64",
        "macos" => "macho64",
        _ => "elf64",
    };

    let out_dir = PathBuf::from(env::var("OUT_DIR").expect("OUT_DIR"));
    let obj = out_dir.join(format!("stitcher_mix.{}.o", target_os));

    // The repo vendors nasm under tools/ on Windows so a local build does not
    // need it on PATH; NASM overrides both.
    let nasm = env::var("NASM").unwrap_or_else(|_| find_nasm(target_os.as_str()));

    let output = Command::new(&nasm)
        .arg(format!("-f{}", format))
        .arg("-o")
        .arg(&obj)
        .arg(&asm)
        .output();

    match output {
        Ok(out) if out.status.success() => {}
        Ok(out) => {
            fail_or_warn(format!(
                "{} failed on {}:\n{}",
                nasm,
                asm.display(),
                String::from_utf8_lossy(&out.stderr)
            ));
            return;
        }
        Err(err) => {
            fail_or_warn(format!(
                "could not run the assembler ({}, {}): {}",
                nasm,
                err,
                asm.display()
            ));
            return;
        }
    }

    // The object is handed straight to the linker.
    //
    // `rustc-link-arg` is documented to apply to benchmarks, binaries, cdylib
    // crates, examples AND tests, which matters here: the test harness also
    // compiles main.rs, so it needs these symbols too. The variant that targets
    // tests alone (`rustc-link-arg-tests`) does not exist, and cargo rejects
    // the whole build with "invalid instruction" when it sees one.
    println!("cargo:rustc-link-arg={}", obj.display());
    println!("cargo:rustc-link-search=native={}", out_dir.display());
    println!("cargo:rustc-cfg=td_stitcher_asm");
}

fn find_nasm(target_os: &str) -> String {
    if target_os == "windows" {
        let vendored = PathBuf::from(env::var("CARGO_MANIFEST_DIR").unwrap_or_default())
            .join("../tools/nasm/nasm.exe");
        if vendored.exists() {
            return vendored.display().to_string();
        }
    }
    "nasm".to_string()
}

/// Panics when the pack build demanded the kernel, warns otherwise.
fn fail_or_warn(message: String) {
    if env::var_os("TD_STITCHER_REQUIRE_ASM").is_some() {
        panic!("{}", message);
    }
    println!("cargo:warning={}", message.replace('\n', " "));
}