! Just tv and make a jpeg - allow min and max params to be sent
!
! E.g. monsta $PRODIR/tvjpeg.pro /tmp/foo.fits
! OR
! E.g. monsta $PRODIR/tvjpeg.pro /tmp/foo.fits -1 10

rd 1 '{arg2}' cfitsio silent
set JPGMIN=-50 JPGMAX=100

! Syntax: monsta tvjpeg.pro infile min max
if argc>2
  set JPGMIN={arg3} JPGMAX={arg4}
end_if

! Write a bonus jpeg, same as output file name but with .jpg appended
abx 1 all median=sky silent
sc 1 sky
tv 1 thresh=JPGMIN sat=JPGMAX cf=bw jpeg={arg2}.jpg silent

end
