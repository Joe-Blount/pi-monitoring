This node declares no resident devices, so it needs no execd stanzas. The
directory exists so that the deployment is the same shape everywhere: the
install script copies whatever is here, and finding nothing is a valid answer
rather than a missing file.
