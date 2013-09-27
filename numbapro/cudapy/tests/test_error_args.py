from .support import testcase, main
from numbapro import vectorize, cuda

def discriminant(a, b, c):
    return a + b + c

@testcase
def test_narg_error():
    sig = ['float32(float32,float32)','float64(float64,float64)']
    try:
        cu_discriminant = vectorize(sig, target='gpu')(discriminant)
    except TypeError, e:
        assert "mismatching # of args" in str(e)
    else:
        raise AssertionError("Excepting an expection")


if __name__ == '__main__':
    main()
