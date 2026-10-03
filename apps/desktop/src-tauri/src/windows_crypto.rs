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
        let success = unsafe {
            CryptProtectData(
                &input,
                std::ptr::null(),
                std::ptr::null(),
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
        let _ = plaintext;
        Err("controller_platform_unsupported")
    }
}

pub(super) fn unprotect_current_user(ciphertext: &[u8]) -> Result<Vec<u8>, &'static str> {
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
        let success = unsafe {
            CryptUnprotectData(
                &input,
                std::ptr::null_mut(),
                std::ptr::null(),
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
        let _ = ciphertext;
        Err("controller_platform_unsupported")
    }
}
