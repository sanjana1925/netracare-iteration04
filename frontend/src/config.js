// Clinic contact details used across the front end (one place to change them).
// The front-desk number is the one in the clinic policy document (a fictional number for the course).
export const CLINIC_PHONE = "1800-000-0000";

// A tel: link that works on phones: digits (and a leading +) only.
export const clinicPhoneHref = (phone = CLINIC_PHONE) => `tel:${phone.replace(/[^\d+]/g, "")}`;
