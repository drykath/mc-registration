from django.core.mail import EmailMessage
from django.db.models import Count
from django.template import loader

from datetime import datetime

from .models import Convention, Payment, Registration

def simple_feistel(value):
    # A simple self-inverse Feistel cipher for ID obfuscation
    # It's good for up to 64-bit inegers. The key is essentially
    # encoded into the r1 mangling function.
    l1 = (value >> 16) & 65535
    r1 = value & 65535

    for i in range(3):
        l2 = r1
        r2 = l1 ^ int((((123 * r1 + 4567) % 8910123) / 654321.0) * 98765)
        l1 = l2
        r1 = r2
    return (r1 << 16) + l1

def stringify_integer(value):
    # Take an integer and encode it as a base(len(alphabet)) string
    # For additional ID obfuscation

    alphabet = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
    base = len(alphabet)
    output = ''

    while value > 0:
        output += alphabet[value%base]
        value //= base

    return output

class PaymentError(Exception):
    pass
