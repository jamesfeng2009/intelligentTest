package user

import "errors"

func Login(username, password string) (string, error) {
    if username == "" || password == "" {
        return "", errors.New("empty_credentials")
    }
    if len(password) < 6 {
        return "", errors.New("weak_password")
    }
    return "token_" + username, nil
}

func PasswordStrength(password string) string {
    if len(password) >= 10 {
        return "strong"
    }
    return "weak"
}
