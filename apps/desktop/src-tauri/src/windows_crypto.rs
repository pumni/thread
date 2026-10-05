pub(super) fn fill_random(output: &mut [u8]) -> Result<(), &'static str> {
    #[cfg(windows)]
    {
        use windows_sys::Win32::Security::Cryptography::{
            BCryptGenRandom, BCRYPT_USE_SYSTEM_PREFERRED_RNG,
        };
        let status = unsafe {
            BCryptGenRandom(
                std::ptr::null_mut(),
                output.as_mut_ptr(),
                output.len() as u32,
                BCRYPT_USE_SYSTEM_PREFERRED_RNG,
            )
        };
        if status == 0 {
            Ok(())
        } else {
            Err("controller_random_source_unavailable")
        }
    }
    #[cfg(not(windows))]
    {
        let _ = output;
        Err("controller_platform_unsupported")
    }
}

pub(super) fn protect_current_user(plaintext: &[u8]) -> Result<Vec<u8>, &'static str> {
    protect_current_user_with_entropy(plaintext, None)
}

fn protect_current_user_with_entropy(
    plaintext: &[u8],
    optional_entropy: Option<&[u8]>,
) -> Result<Vec<u8>, &'static str> {
    #[cfg(windows)]
    {
        use windows_sys::Win32::{
            Foundation::LocalFree,
            Security::Cryptography::{
                CryptProtectData, CRYPTPROTECT_UI_FORBIDDEN, CRYPT_INTEGER_BLOB,
            },
        };

        let input = CRYPT_INTEGER_BLOB {
            cbData: plaintext.len() as u32,
            pbData: plaintext.as_ptr() as *mut u8,
        };
        let mut output = CRYPT_INTEGER_BLOB {
            cbData: 0,
            pbData: std::ptr::null_mut(),
        };
        let entropy_blob = optional_entropy.map(|entropy| CRYPT_INTEGER_BLOB {
            cbData: entropy.len() as u32,
            pbData: entropy.as_ptr() as *mut u8,
        });
        let entropy_pointer = entropy_blob
            .as_ref()
            .map_or(std::ptr::null(), |blob| blob as *const _);
        let success = unsafe {
            CryptProtectData(
                &input,
                std::ptr::null(),
                entropy_pointer,
                std::ptr::null_mut(),
                std::ptr::null(),
                CRYPTPROTECT_UI_FORBIDDEN,
                &mut output,
            )
        };
        if success == 0 || output.pbData.is_null() {
            return Err("controller_dpapi_protect_failed");
        }
        let protected =
            unsafe { std::slice::from_raw_parts(output.pbData, output.cbData as usize).to_vec() };
        unsafe { LocalFree(output.pbData.cast()) };
        Ok(protected)
    }
    #[cfg(not(windows))]
    {
        let _ = (plaintext, optional_entropy);
        Err("controller_platform_unsupported")
    }
}

pub(super) fn unprotect_current_user(ciphertext: &[u8]) -> Result<Vec<u8>, &'static str> {
    unprotect_current_user_with_entropy(ciphertext, None)
}

fn unprotect_current_user_with_entropy(
    ciphertext: &[u8],
    optional_entropy: Option<&[u8]>,
) -> Result<Vec<u8>, &'static str> {
    #[cfg(windows)]
    {
        use windows_sys::Win32::{
            Foundation::LocalFree,
            Security::Cryptography::{
                CryptUnprotectData, CRYPTPROTECT_UI_FORBIDDEN, CRYPT_INTEGER_BLOB,
            },
        };

        let input = CRYPT_INTEGER_BLOB {
            cbData: ciphertext.len() as u32,
            pbData: ciphertext.as_ptr() as *mut u8,
        };
        let mut output = CRYPT_INTEGER_BLOB {
            cbData: 0,
            pbData: std::ptr::null_mut(),
        };
        let entropy_blob = optional_entropy.map(|entropy| CRYPT_INTEGER_BLOB {
            cbData: entropy.len() as u32,
            pbData: entropy.as_ptr() as *mut u8,
        });
        let entropy_pointer = entropy_blob
            .as_ref()
            .map_or(std::ptr::null(), |blob| blob as *const _);
        let success = unsafe {
            CryptUnprotectData(
                &input,
                std::ptr::null_mut(),
                entropy_pointer,
                std::ptr::null_mut(),
                std::ptr::null(),
                CRYPTPROTECT_UI_FORBIDDEN,
                &mut output,
            )
        };
        if success == 0 || output.pbData.is_null() {
            return Err("controller_dpapi_unprotect_failed");
        }
        let plaintext =
            unsafe { std::slice::from_raw_parts(output.pbData, output.cbData as usize).to_vec() };
        unsafe { std::ptr::write_bytes(output.pbData, 0, output.cbData as usize) };
        unsafe { LocalFree(output.pbData.cast()) };
        Ok(plaintext)
    }
    #[cfg(not(windows))]
    {
        let _ = (ciphertext, optional_entropy);
        Err("controller_platform_unsupported")
    }
}

pub(super) fn validate_worker_device_key(
    ciphertext: &[u8],
    entropy: &[u8],
) -> Result<(), WorkerKeyError> {
    let mut plaintext = unprotect_current_user_with_entropy(ciphertext, Some(entropy))
        .map_err(|_| WorkerKeyError::UnprotectFailed)?;
    // Ed25519 PKCS#8 DER PrivateKeyInfo is a fixed 48-byte structure. Validate
    // its algorithm and inner key shape without returning the key material.
    const ED25519_PKCS8_PREFIX: [u8; 16] = [
        0x30, 0x2e, 0x02, 0x01, 0x00, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x70, 0x04, 0x22, 0x04,
        0x20,
    ];
    let valid = plaintext.len() == 48 && plaintext.starts_with(&ED25519_PKCS8_PREFIX);
    plaintext.zeroize();
    if valid {
        Ok(())
    } else {
        Err(WorkerKeyError::InvalidPayload)
    }
}

#[cfg(all(test, windows))]
mod tests {
    use super::*;

    #[test]
    fn worker_dpapi_validation_uses_entropy_and_does_not_change_protected_bytes() {
        let mut private_key = vec![
            0x30, 0x2e, 0x02, 0x01, 0x00, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x70, 0x04, 0x22,
            0x04, 0x20,
        ];
        private_key.extend_from_slice(&[0x42; 32]);
        let entropy = b"threads-platform-worker-key-v1\0synthetic-worker-id";
        let protected = protect_current_user_with_entropy(&private_key, Some(entropy))
            .expect("protect synthetic Worker key");
        private_key.zeroize();

        let before = protected.clone();
        assert!(validate_worker_device_key(&protected, entropy).is_ok());
        assert!(matches!(
            validate_worker_device_key(&protected, b"wrong entropy"),
            Err(WorkerKeyError::UnprotectFailed)
        ));
        assert_eq!(protected, before);
    }
}
use zeroize::Zeroize;

pub(super) enum WorkerKeyError {
    UnprotectFailed,
    InvalidPayload,
}
