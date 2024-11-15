import torch
import torch.nn as nn

from torchlensmaker.shapes.common import normed


class Line:

    def __init__(self, lens_radius, init=None, share=None, scale=1.0, optimize=True):
        if init is not None and share is None:
            init = torch.as_tensor(init)
            assert init.shape == (3,)
            if optimize:
                self.params = {
                    "abc": nn.Parameter(init)
                }
            else:
                self.params = {
                    "abc": init
                }
        else:
            raise NotImplementedError("")

        self.lens_radius = lens_radius
        self._share = share
        self._scale = scale
    
    def parameters(self):
        return self.params
    
    def coefficients(self):
        if self._share is None:
            return self.params["abc"]
        else:
            raise NotImplementedError("")
    
    def domain(self):
        return torch.tensor([-self.lens_radius, self.lens_radius])

    def evaluate(self, X):
        X = torch.atleast_1d(torch.as_tensor(X))
        # TODO doesn't work for vertical lines
        # TODO switch to parametric representation
        a, b, c = self.coefficients()
        Y = -a / b * X - c / b
        return torch.stack([X, Y], dim=-1)

    def normal(self, points):
        coefficients = self.coefficients()
        return torch.tile(normed(coefficients[:2]), (points.shape[0], 1))

    def intersect_batch(self, lines):
        """
        Intersect with multiple lines where lines is a tensor of shape (N, 3) 
        representing N lines in the form [a, b, c] coefficients.
        """
        # Ensure lines is a tensor
        lines = torch.as_tensor(lines)
        
        # Extract coefficients
        a1, b1, c1 = self.coefficients()
        a2, b2, c2 = lines[:, 0], lines[:, 1], lines[:, 2]
        
        # Compute determinant
        det = a1 * b2 - a2 * b1
        
        # Prepare the result tensor
        result = torch.zeros((lines.shape[0], 2), dtype=torch.float32)
        
        # Compute intersection points where det != 0
        valid = torch.abs(det) >= 1e-8
        x = torch.zeros_like(det)
        y = torch.zeros_like(det)
        
        x[valid] = (b1 * c2[valid] - b2[valid] * c1) / det[valid]
        y[valid] = (c1 * a2[valid] - c2[valid] * a1) / det[valid]
        
        result[:, 0] = x
        result[:, 1] = y
        
        # Set intersection to [inf, inf] where lines are parallel
        result[~valid] = float('inf')
        
        return result
    
    def collide(self, lines):
        points = self.intersect_batch(lines)
        normals = self.normal(points)
        return points, normals
